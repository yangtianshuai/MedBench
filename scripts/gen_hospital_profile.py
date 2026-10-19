# -*- coding: utf-8 -*-
"""生成院方业务配置 hospital_profile.yaml（科室分组 + 口径规则）。

为什么用生成而不是手写：
  统计科室有 105 个、HIS 科室几百个，手写必然出错且不可复现。
  从 HIS 读出来生成一份「预置」，院方再按自己的口径改 —— 改完
  跑 scripts/reconcile_his.py 就能看到差异是否归零。

配置的两块内容：
  1. dept_groups  科室分组：把若干 HIS 科室归到一个组
                  （统计科室只是其中一种；院方可加「内科系统」「重点专科群」等）
  2. caliber_rules 口径规则：哪些算、哪些不算（如出院人次是否计入新生儿）
                  每条规则本质是给指定指标追加一段 WHERE
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import oracledb  # noqa: E402

OUT_PATH = os.path.join(_ROOT, "semantic-layer", "hospital_profile.yaml")

HEADER = """# 院方业务配置：科室分组 + 口径规则
#
# 这一层是**给医院管理员改的**，不在代码里，也不在指标定义里。
# 由 scripts/gen_hospital_profile.py 生成预置（从 HIS 读），之后手改即可。
# 改完执行 scripts/reconcile_his.py 验证口径是否对上。
#
# ⚠️ 分组与规则都是「本院口径」，不同医院不同；换医院重新生成本文件。

hospital: 院内 HIS 镜像
generated_by: scripts/gen_hospital_profile.py
generated_at: {ts}

# ── 一、科室分组 ──────────────────────────────────────
# 语义层 hierarchy 里声明的 dept_group 在这里落地。
# 分组 = 一组 HIS 科室代码；可任意自定义，不限于统计科室。
dept_groups:
{dept_groups}

# ── 二、口径规则 ──────────────────────────────────────
# 每条规则 = 给指定指标追加一段 WHERE 条件。
# enabled: true 才生效。默认全部关闭 —— 本院口径由院方拍板，系统不替他决定。
caliber_rules:
{caliber_rules}
"""

# 预置的口径规则：都是 HIS 报表对账时发现的分歧点，
# 但没有一条是「系统该替院方决定」的，所以默认关闭。
#
# ★ where 里用 {逻辑列名} 占位符，不要写死物理列名 —— 否则同一份规则
#   在直连 HIS（NEWBORN）和 ADS 宽表（newborn）上只有一边能跑。
#   占位符在加载时按数据源替换：直连用物理列名，ADS 用逻辑列名。
PRESET_RULES = [
    {
        "code": "exclude_newborn",
        "name": "出院人次不计新生儿",
        "enabled": False,
        "applies_to": ["ip_discharge_cnt", "ip_admit_cnt",
                       "inpatient_death_cnt", "ip_los_avg"],
        "where": "({newborn} IS NULL OR {newborn} NOT IN ('Y', 'B', '1'))",
        "note": ("对账发现：产科 2023 出院 10,533，HIS 报表同格 5,680。"
                 "NEWBORN 有 Y/B/1 三种非空取值（另有 NULL）。"
                 "★ 实测开启后全院差异反而扩大（-5,804 → -17,537），"
                 "说明 HIS 报表是计入新生儿的，本规则默认不开。"),
    },
    {
        "code": "inpatient_only",
        "name": "只统计住院科室（排除门诊科室）",
        "enabled": False,
        "applies_to": ["ip_discharge_cnt", "ip_admit_cnt",
                       "inpatient_death_cnt", "ip_los_avg"],
        "where": ("({discharge_dept_code} IN "
                  "(SELECT DEPT_CODE FROM COMM.DEPT_DICT WHERE OUTP_OR_INP = 1))"),
        "his_only": True,
        "note": ("对账发现 1.1.1.1 / 1.1.1.2 映射到的是门诊科室"
                 "（神经内科门诊、心血管门诊），却计入了住院出院人次。"
                 "★ his_only：规则里引用了 HIS 的 COMM.DEPT_DICT，ADS 上无法执行。"),
    },
]


def _yaml_rules() -> str:
    out = []
    for r in PRESET_RULES:
        applies = ", ".join(f'"{c}"' for c in r["applies_to"])
        his_only = "\n    his_only: true" if r.get("his_only") else ""
        out.append(f"""  - code: {r['code']}
    name: {r['name']}
    enabled: {'true' if r['enabled'] else 'false'}
    applies_to: [{applies}]
    where: "{r['where']}"{his_only}
    note: >-
      {r['note']}""")
    return "\n".join(out)


def _yaml_groups(mapping: dict) -> str:
    """mapping: {stat_dept_code: [his_dept_code, ...]}"""
    out = []
    out.append("""  - code: stat_dept
    name: 统计科室
    source: HIS 统计科室口径（REPORT.DEPT_VS_STAT_DEPT）
    from_dept_basis: discharge
    default: 未分组
    groups:""")
    for stat in sorted(mapping):
        depts = sorted(mapping[stat])
        if len(depts) == 1:
            out.append(f'      "{stat}": ["{depts[0]}"]')
        else:
            lst = ", ".join(f'"{d}"' for d in depts)
            out.append(f'      "{stat}": [{lst}]')
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成院方业务配置")
    ap.add_argument("--out", default=OUT_PATH, help="输出路径")
    args = ap.parse_args()

    if os.getenv("ASK_HIS_THICK") == "1":
        oracledb.init_oracle_client()
    conn = oracledb.connect(
        user=os.getenv("ASK_HIS_USER", "his"),
        password=os.getenv("ASK_HIS_PASSWORD", ""),
        dsn=oracledb.makedsn(os.getenv("ASK_HIS_HOST", "10.1.33.46"),
                             1521, service_name="oracle10"))
    cur = conn.cursor()

    print("[1/2] 读取 REPORT.DEPT_VS_STAT_DEPT ...")
    t0 = time.time()
    cur.execute("""SELECT STAT_DEPT_CODE, DEPT_CODE
FROM REPORT.DEPT_VS_STAT_DEPT
WHERE STAT_DEPT_CODE IS NOT NULL""")
    mapping: dict = defaultdict(list)
    for stat, dept in cur.fetchall():
        if stat and dept:
            mapping[stat].append(str(dept).strip())
    print(f"      {len(mapping)} 个统计科室，"
          f"{sum(len(v) for v in mapping.values())} 条映射，用时 {time.time()-t0:.1f}s")

    print("[2/2] 写入配置 ...")
    content = HEADER.format(
        ts=time.strftime("%Y-%m-%d %H:%M:%S"),
        dept_groups=_yaml_groups(mapping),
        caliber_rules=_yaml_rules(),
    )
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"      已写入：{args.out}")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
