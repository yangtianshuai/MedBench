# -*- coding: utf-8 -*-
"""口径对账（F5.7）：语义层算的值 vs HIS 厂商报表 REPORT.RPT_DEPT_MONTH。

为什么必须有这个工具：
    HIS 厂商自己的报表就是它对指标口径的实现。拿它跟我们算的对，
    对得上 = 口径确认；对不上 = 差异本身就是口径争议的证据，
    拿着它去跟医务处开口径确认，比开会争论快得多。

用法：
    python scripts/reconcile_his.py --year 2023
    python scripts/reconcile_his.py --year 2023 --json out.json

设计要点：
    1) 统计科室有层级（父 = 子之和），直接 SUM 全部会重复计入，
       对账两侧都只取「叶子科室」再比。
    2) 我方 HIS 科室代码 → 统计科室代码，走 REPORT.DEPT_VS_STAT_DEPT。
    3) 两项都从真实库读，不缓存、不假设。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import oracledb
from app.config import settings

# ── 对账项：我方指标 ↔ HIS 报表指标代码 ──────────────────────
# 来源：REPORT.REPORT_STAT_ITEMS（REPORT_CODE='005'）
RECON_ITEMS = [
    {
        "metric": "ip_admit_cnt",
        "name": "入院人次",
        "item_code": "1.1.2.05",        # HIS: 入院病人数
        "item_name": "入院病人数",
        "table": "MEDREC.PAT_VISIT",
        "date_col": "ADMISSION_DATE_TIME",
        "dept_col": "DEPT_ADMISSION_TO",   # 按入院科室（与入院口径一致）
        "expr": "COUNT(VISIT_ID)",
    },
    {
        "metric": "ip_discharge_cnt",
        "name": "出院人次",
        "item_code": "1.1.2.06",        # HIS: 出院病人数
        "item_name": "出院病人数",
        "table": "MEDREC.PAT_VISIT",
        "date_col": "DISCHARGE_DATE_TIME",
        "dept_col": "DEPT_DISCHARGE_FROM", # 按出院科室
        "expr": "COUNT(VISIT_ID)",
    },
]

# 容差：人次级整数指标允许 ±1（跨月边界/时间精度导致）
ABS_TOL = 1.0
REL_TOL = 0.005   # 0.5%


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


def leaf_codes(codes: List[str]) -> List[str]:
    """保留不是任何其他编码前缀的编码（叶子），剔除父级避免重复求和。"""
    s = {c for c in codes if c}
    out = []
    for c in s:
        if not any(o != c and o.startswith(c + ".") for o in s):
            out.append(c)
    return sorted(out)


def load_profile() -> Dict[str, Any]:
    """读院方业务配置，使对账与线上语义层用同一套口径。

    这一步是「配完即验」的关键：否则改了 hospital_profile.yaml，
    线上出数变了而对账结果不变，闭环就断了。
    """
    import yaml
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "semantic-layer", "hospital_profile.yaml")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def caliber_where(profile: Dict[str, Any], metric_code: str) -> List[str]:
    """取该指标当前启用的口径规则（判定逻辑与语义层一致）。"""
    out = []
    for r in (profile.get("caliber_rules") or []):
        if r.get("enabled") and metric_code in (r.get("applies_to") or []):
            out.append(r["where"])
    return out


def group_case(profile: Dict[str, Any], dept_col: str) -> Optional[str]:
    """院方配置的科室分组 → CASE 表达式；无配置则返回 None（回退 HIS 映射表）。"""
    for g in (profile.get("dept_groups") or []):
        groups = g.get("groups") or {}
        if not groups:
            continue
        parts = []
        for name, depts in groups.items():
            if not depts:
                continue
            inlist = ", ".join(f"'{d}'" for d in depts)
            parts.append(f"WHEN v.{dept_col} IN ({inlist}) THEN '{name}'")
        return ("CASE " + " ".join(parts)
                + f" ELSE '{g.get('default') or '未分组'}' END")
    return None


def fetch_ours(cur, item: Dict[str, Any], year: int,
               profile: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """我方：按院方配置的科室分组聚合（无配置时回退 HIS 映射表）。"""
    profile = profile or {}
    conds = [f"v.{item['date_col']} >= DATE '{year}-01-01'",
             f"v.{item['date_col']} <  DATE '{year + 1}-01-01'"]
    conds += caliber_where(profile, item["metric"])

    case_expr = group_case(profile, item["dept_col"])
    if case_expr:
        # Oracle 不支持 GROUP BY 别名，表达式要写两遍
        sql = f"""
SELECT {case_expr} AS stat_dept, {item['expr']} AS v
FROM {item['table']} v
WHERE {' AND '.join(conds)}
GROUP BY {case_expr}"""
    else:
        sql = f"""
SELECT m.STAT_DEPT_CODE AS stat_dept, {item['expr']} AS v
FROM {item['table']} v
LEFT JOIN REPORT.DEPT_VS_STAT_DEPT m ON m.DEPT_CODE = v.{item['dept_col']}
WHERE {' AND '.join(conds)}
GROUP BY m.STAT_DEPT_CODE"""
    cur.execute(sql)
    out: Dict[str, float] = {}
    for dept, val in cur.fetchall():
        if dept is None:
            dept = "(未映射)"
        out[dept] = float(val or 0)
    return out


def fetch_his(cur, item: Dict[str, Any], year: int) -> Dict[str, float]:
    """HIS 报表：RPT_DEPT_MONTH 同指标同年份。"""
    sql = f"""
SELECT DEPT_CODE, SUM(STAT_DATA) AS v
FROM REPORT.RPT_DEPT_MONTH
WHERE RPT_Y = '{year}' AND ITEM_CODE = '{item['item_code']}'
GROUP BY DEPT_CODE"""
    cur.execute(sql)
    return {d: float(v or 0) for d, v in cur.fetchall() if d}


def compare(ours: Dict[str, float], his: Dict[str, float]) -> Dict[str, Any]:
    ours_leaf = leaf_codes(list(ours.keys()))
    his_leaf = leaf_codes(list(his.keys()))

    o_sum = sum(ours[c] for c in ours_leaf)
    h_sum = sum(his[c] for c in his_leaf)

    rows: List[Dict[str, Any]] = []
    for c in sorted(set(ours_leaf) | set(his_leaf)):
        o = ours.get(c, 0.0)
        h = his.get(c, 0.0)
        d = o - h
        rel = (abs(d) / h) if h else (0.0 if not o else 1.0)
        ok = abs(d) <= ABS_TOL or rel <= REL_TOL
        rows.append({"stat_dept": c, "ours": o, "his": h, "diff": d,
                     "rel": rel, "match": ok})

    matched = sum(1 for r in rows if r["match"])
    return {
        "ours_total": o_sum,
        "his_total": h_sum,
        "diff": o_sum - h_sum,
        "rel": (abs(o_sum - h_sum) / h_sum) if h_sum else 0.0,
        "dept_rows": rows,
        "dept_matched": matched,
        "dept_total": len(rows),
        "ours_leaf_cnt": len(ours_leaf),
        "his_leaf_cnt": len(his_leaf),
    }


def render_md(report: List[Dict[str, Any]], year: int) -> str:
    """渲染口径确认单。

    这份东西是给医务处看的，不是给工程师看的：
    只讲「哪个科室、差多少、可能的口径分歧在哪、请确认什么」，
    不出现 SQL、指标代码以外的内部术语。
    """
    import datetime as _dt
    L: List[str] = []
    L.append(f"# 住院域指标口径确认单（{year} 年）")
    L.append("")
    L.append(f"- 生成时间：{_dt.datetime.now():%Y-%m-%d %H:%M}")
    L.append("- 生成工具：`scripts/reconcile_his.py`（可复现，改年份即可重出）")
    L.append("- 对账双方：**本系统语义层计算值** vs **HIS 厂商报表 `RPT_DEPT_MONTH`**")
    L.append("")
    L.append("## 一、结论")
    L.append("")
    for it in report:
        ok = abs(it["diff"]) <= ABS_TOL or it["rel"] <= REL_TOL
        L.append(f"- **{it['name']}**：我方 {it['ours_total']:,.0f} / "
                 f"HIS 报表 {it['his_total']:,.0f}，"
                 f"差 {it['diff']:,.0f}（{it['rel'] * 100:.2f}%）—— "
                 f"{'一致' if ok else '需确认'}")
        L.append(f"  - 科室级：{it['dept_matched']}/{it['dept_total']} 个统计科室对上")
    L.append("")

    L.append("## 二、需确认的统计科室")
    L.append("")
    L.append("> 说明：以下差异**不是计数逻辑错误**，而是「哪些 HIS 科室归属到哪个统计科室」"
             "的映射口径不同。同一批患者，两边算的总量能对上，只是归到不同篮子里。")
    L.append("")

    idx = 0
    for it in report:
        bad = [r for r in it["dept_rows"] if not r["match"]]
        if not bad:
            continue
        for r in sorted(bad, key=lambda x: -abs(x["diff"])):
            idx += 1
            o, h, d = r["ours"], r["his"], r["diff"]
            L.append(f"### {idx}. 统计科室 `{r['stat_dept']}`（{it['name']}）")
            L.append("")
            L.append(f"- 我方：{o:,.0f}　HIS 报表：{h:,.0f}　差异：{d:+,.0f}")
            L.append("")
            if o == 0:
                L.append("- **现象**：HIS 报表在该统计科室有数据，我方为 0 —— "
                         "没有任何 HIS 科室映射到这个统计科室。")
                L.append("- **可能原因**：该统计科室是历史/汇总口径（如某病区、某合并分组），"
                         "`DEPT_VS_STAT_DEPT` 映射表中当前无 HIS 科室指向它。")
                L.append("- **请确认**：该统计科室在 "
                         f"{year} 年是否仍在用？对应哪些 HIS 科室（或哪些病区）？")
            else:
                L.append("- **现象**：我方比 HIS 报表多 —— 一个 HIS 科室的患者被我方全部计入"
                         "该统计科室，而报表只计入其中一部分。")
                L.append("- **可能原因**：该 HIS 科室内部按病种/病区/母婴等规则被拆分统计，"
                         "`DEPT_VS_STAT_DEPT` 是一对一映射，未体现拆分规则。")
                L.append("- **请确认**：该 HIS 科室的出院患者，应按什么规则拆分到统计科室？")
            L.append("")
            L.append("**待选口径**（请勾选或补充）：")
            L.append("")
            L.append("1. 以 HIS 报表口径为准（我方补齐映射规则）")
            L.append("2. 以 HIS 科室一对一映射为准（报表侧调整）")
            L.append("3. 其他（请注明）____________")
            L.append("")

    L.append("## 三、已对上的部分（无需确认）")
    L.append("")
    for it in report:
        good = [r for r in it["dept_rows"] if r["match"]]
        L.append(f"- **{it['name']}**：{len(good)} 个统计科室逐位一致，"
                 f"合计 {sum(r['ours'] for r in good):,.0f}")
    L.append("")
    L.append("---")
    L.append("")
    L.append("**签认**：医务处 ____________　信息科 ____________　日期 ____________")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="口径对账：语义层 vs HIS 厂商报表")
    ap.add_argument("--year", type=int, default=2023, help="对账年份（默认 2023，归档完整）")
    ap.add_argument("--json", dest="json_out", default="", help="同时输出 JSON 报告")
    ap.add_argument("--md", dest="md_out", default="",
                    help="输出给医务处的「口径确认单」Markdown")
    args = ap.parse_args()

    profile = load_profile()
    rules_on = [r.get("code") for r in (profile.get("caliber_rules") or [])
                if r.get("enabled")]
    print(f"院方配置：科室分组 {len(profile.get('dept_groups') or [])} 组"
          f"｜启用口径规则：{rules_on or '（无，按原始口径）'}")

    conn = connect()
    cur = conn.cursor()

    print("=" * 78)
    print(f"口径对账 · {args.year} 年 · 语义层 vs HIS 报表 REPORT.RPT_DEPT_MONTH")
    print("=" * 78)

    report: List[Dict[str, Any]] = []
    overall_ok = True

    for item in RECON_ITEMS:
        ours = fetch_ours(cur, item, args.year, profile)
        his = fetch_his(cur, item, args.year)
        if not his:
            print(f"\n[{item['name']}] HIS 报表无该指标（{item['item_code']}），跳过")
            continue
        res = compare(ours, his)
        res["metric"] = item["metric"]
        res["name"] = item["name"]
        res["item_code"] = item["item_code"]
        res["item_name"] = item["item_name"]
        report.append(res)

        verdict = "一致 ✅" if abs(res["diff"]) <= ABS_TOL or res["rel"] <= REL_TOL else "需确认 ⚠️"
        if verdict != "一致 ✅":
            overall_ok = False

        print(f"\n【{item['name']}】我方 measure={item['metric']}  "
              f"HIS 报表={item['item_code']}（{item['item_name']}）")
        print(f"  我方（叶子科室合计）: {res['ours_total']:>12,.0f}")
        print(f"  HIS 报表（叶子合计）: {res['his_total']:>12,.0f}")
        print(f"  差异               : {res['diff']:>12,.0f}  "
              f"({res['rel'] * 100:.2f}%)   {verdict}")
        print(f"  科室级             : {res['dept_matched']}/{res['dept_total']} 个统计科室对上")

        bad = [r for r in res["dept_rows"] if not r["match"]]
        if bad:
            print("  差异明细（按差异绝对值降序）:")
            for r in sorted(bad, key=lambda x: -abs(x["diff"]))[:10]:
                print(f"    {r['stat_dept']:<14} 我方 {r['ours']:>9,.0f}  "
                      f"HIS {r['his']:>9,.0f}  差 {r['diff']:>9,.0f}")

    print("\n" + "=" * 78)
    print("总结：" + ("全部对上，口径可用 ✅" if overall_ok else "存在差异，需开口径确认 ⚠️"))
    print("=" * 78)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"year": args.year, "items": report}, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 报告已写入：{args.json_out}")

    if args.md_out:
        with open(args.md_out, "w", encoding="utf-8") as f:
            f.write(render_md(report, args.year))
        print(f"口径确认单已写入：{args.md_out}")

    cur.close()
    conn.close()
    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
