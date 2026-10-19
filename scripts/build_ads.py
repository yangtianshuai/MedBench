# -*- coding: utf-8 -*-
"""A 路线：从 HIS 抽取住院域数据，构建 ADS 宽表 dwd_fact_inpatient_discharge。

设计契约（这是本脚本存在的理由）：
    语义层 metric_registry.yaml 里的逻辑表名就是 dwd_fact_inpatient_discharge。
    因此 ADS 建成后，**语义层与物理映射均可零改动**，只需把 executor 切到 ADS。
    如果建完 ADS 还要改指标定义，说明设计失败了。

本地验证用 SQLite（Python 内置，内网无需装包）；
生产环境换成 Doris，只要字段一致，语义层同样零改动（DDL 一并产出）。

用法：
    python scripts/build_ads.py                    # 抽住院主记录（快）
    python scripts/build_ads.py --with-fee         # 叠加费用聚合（慢，千万级明细）
    python scripts/build_ads.py --since 2023-01-01

产物：
    var/ads.db            表 dwd_fact_inpatient_discharge
    var/ads_doris.sql     生产环境（Doris）建表 DDL
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oracledb
from app.config import settings

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADS_PATH = os.path.join(_ROOT, "var", "ads.db")

# ── 目标宽表（SQLite 版）────────────────────────────────────
DDL_SQLITE = """
DROP TABLE IF EXISTS dwd_fact_inpatient_discharge;
CREATE TABLE dwd_fact_inpatient_discharge (
    visit_id             TEXT,     -- 就诊标识（住院次数）
    patient_id           TEXT,     -- 患者标识
    discharge_dept_code  TEXT,     -- 出院科室（口径：discharge）
    admit_dept_code      TEXT,     -- 入院科室（口径：admit）
    admit_date           TEXT,     -- YYYY-MM-DD
    discharge_date       TEXT,     -- YYYY-MM-DD
    settle_date          TEXT,     -- 结算日期：镜像无独立结算日，落回出院日
    occupy_bed_days      REAL,     -- 预计算：出院 - 入院（天）
    is_death_in_hospital INTEGER,  -- 院内死亡标志（0/1）
    newborn              TEXT,     -- 新生儿标志（Y/B/1，NULL=非新生儿）
    total_fee            REAL,     -- 住院总收入（--with-fee 时填充）
    drug_fee             REAL,     -- 药品费（病案首页口径 F*/G*）
    material_fee         REAL,     -- 耗材费（病案首页口径 I*）
    data_maturity        TEXT      -- settled / provisional
)
"""

# 生产环境同构 DDL（Doris）
DDL_DORIS = """-- 生产环境（Doris）建表 DDL，由 scripts/build_ads.py 生成
-- 只要字段与本表一致，语义层即可零改动
CREATE TABLE IF NOT EXISTS dwd_fact_inpatient_discharge (
    visit_id             VARCHAR(64)  COMMENT '就诊标识（住院次数）',
    patient_id           VARCHAR(64)  COMMENT '患者标识',
    discharge_dept_code  VARCHAR(32)  COMMENT '出院科室（口径：discharge）',
    admit_dept_code      VARCHAR(32)  COMMENT '入院科室（口径：admit）',
    admit_date           DATE         COMMENT '入院日期',
    discharge_date       DATE         COMMENT '出院日期',
    settle_date          DATE         COMMENT '结算日期（镜像无独立结算日，落回出院日）',
    occupy_bed_days      DOUBLE       COMMENT '出院者占用床日（预计算）',
    is_death_in_hospital INT          COMMENT '院内死亡标志 0/1',
    newborn              VARCHAR(8)   COMMENT '新生儿标志（Y/B/1，NULL=非新生儿）',
    total_fee            DOUBLE       COMMENT '住院总收入',
    drug_fee             DOUBLE       COMMENT '药品费（病案首页口径 F*/G*）',
    material_fee         DOUBLE       COMMENT '耗材费（病案首页口径 I*）',
    data_maturity        VARCHAR(16)  COMMENT 'settled / provisional'
)
DUPLICATE KEY(visit_id)
COMMENT '住院域出院事实宽表'
DISTRIBUTED BY HASH(visit_id) BUCKETS 8;
"""


def connect():
    pw = os.getenv("ASK_HIS_PASSWORD", "")
    if not pw:
        raise SystemExit("需要设置环境变量 ASK_HIS_PASSWORD")
    oracledb.init_oracle_client()
    return oracledb.connect(
        user=settings.his_user,
        password=pw,
        dsn=oracledb.makedsn(settings.his_host, settings.his_port, service_name=settings.his_service),
    )


def fetch_visits(cur, since: str) -> List[tuple]:
    print(f"[1/3] 抽取 MEDREC.PAT_VISIT（出院日期 >= {since}）...")
    t0 = time.time()
    cur.execute(f"""
SELECT VISIT_ID, PATIENT_ID,
       DEPT_DISCHARGE_FROM, DEPT_ADMISSION_TO,
       ADMISSION_DATE_TIME, DISCHARGE_DATE_TIME, DEATH_DATE_TIME, NEWBORN
FROM MEDREC.PAT_VISIT
WHERE DISCHARGE_DATE_TIME >= DATE '{since}'""")
    rows = cur.fetchall()
    print(f"      {len(rows):,} 行，用时 {time.time() - t0:.1f}s")
    return rows


def fetch_fees(cur, since: str) -> Dict[str, tuple]:
    """按 (PATIENT_ID, VISIT_ID) 聚合住院费用（总额 / 药品 / 耗材）。

    ⚠️ INP_BILL_DETAIL 5550 万行，GROUP BY 是重活 —— 这正是必须建 ADS 的原因：
    直连查一次要 18~24 秒，用户等不了；预聚合进宽表后是毫秒级。

    分类口径：病案首页分类 CLASS_ON_MR（F*=西药、G*=中药、I*=耗材）。
    与 physical_map_his.yaml 保持一致；若院方改签认为财务口径，此处同步改。
    """
    print(f"[2/3] 抽取 INPBILL.INP_BILL_DETAIL 费用聚合（>= {since}）...")
    t0 = time.time()
    cur.execute(f"""
SELECT PATIENT_ID || '|' || TO_CHAR(VISIT_ID) AS vk,
       SUM(COSTS) AS fee,
       SUM(CASE WHEN CLASS_ON_MR LIKE 'F%' OR CLASS_ON_MR LIKE 'G%'
                THEN COSTS ELSE 0 END) AS drug,
       SUM(CASE WHEN CLASS_ON_MR LIKE 'I%' THEN COSTS ELSE 0 END) AS mat
FROM INPBILL.INP_BILL_DETAIL
WHERE BILLING_DATE_TIME >= DATE '{since}'
GROUP BY PATIENT_ID, VISIT_ID""")
    out = {r[0]: (float(r[1] or 0), float(r[2] or 0), float(r[3] or 0))
           for r in cur.fetchall()}
    print(f"      {len(out):,} 个就诊的费用，用时 {time.time() - t0:.1f}s")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="构建住院域 ADS 宽表")
    ap.add_argument("--since", default="2023-01-01", help="起始出院日期")
    ap.add_argument("--with-fee", action="store_true", help="叠加费用聚合（较慢）")
    ap.add_argument("--out", default=ADS_PATH, help="SQLite ADS 文件路径")
    args = ap.parse_args()

    conn = connect()
    cur = conn.cursor()
    visits = fetch_visits(cur, args.since)
    fees: Dict[str, float] = {}
    if args.with_fee:
        fees = fetch_fees(cur, args.since)
    cur.close()
    conn.close()

    print("[3/3] 写入 ADS 宽表 ...")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    con = sqlite3.connect(args.out)
    con.executescript(DDL_SQLITE)

    recs = []
    for (visit_id, patient_id, d_dept, a_dept, adm, dis, death, newborn) in visits:
        occ = None
        if adm and dis:
            occ = (dis - adm).total_seconds() / 86400.0
        vk = f"{patient_id}|{visit_id}"
        recs.append((
            str(visit_id) if visit_id is not None else None,
            patient_id, d_dept, a_dept,
            adm.strftime("%Y-%m-%d") if adm else None,
            dis.strftime("%Y-%m-%d") if dis else None,
            dis.strftime("%Y-%m-%d") if dis else None,   # settle_date 落回出院日
            occ,
            1 if death else 0,
            (str(newborn).strip() if newborn else None),
            *((fees.get(vk)) or (None, None, None)),
            "settled" if dis else "provisional",
        ))

    con.executemany(
        "INSERT INTO dwd_fact_inpatient_discharge VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", recs)
    con.commit()

    n = con.execute("SELECT COUNT(*) FROM dwd_fact_inpatient_discharge").fetchone()[0]
    print(f"      dwd_fact_inpatient_discharge: {n:,} 行")
    span = con.execute(
        "SELECT MIN(discharge_date), MAX(discharge_date) "
        "FROM dwd_fact_inpatient_discharge").fetchone()
    print(f"      出院日期跨度：{span[0]} ~ {span[1]}")

    ddl_path = os.path.join(os.path.dirname(args.out), "ads_doris.sql")
    with open(ddl_path, "w", encoding="utf-8") as f:
        f.write(DDL_DORIS)
    print(f"      Doris DDL 已写入：{ddl_path}")

    con.close()
    print("\n完成。下一步：把 executor 切到 ADS，验证语义层零改动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
