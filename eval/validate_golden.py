#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Golden 集标注质量校验脚本

用法:
    # 基本校验（静态）
    python validate_golden.py --input golden_set_v1.csv

    # 校验指标编码与语义层注册表对齐
    python validate_golden.py --input golden_set_v1.csv \
        --registry ../semantic-layer/metric_registry.yaml

    # 校验歧义题的 clarify_resolution 能否被语义层写回（建议必开）
    python validate_golden.py --input golden_set_v1.csv \
        --ambiguity ../semantic-layer/ambiguity_rules.yaml

    # 连库执行 expected_sql（可抓出写错的 SQL）
    python validate_golden.py --input golden_set_v1.csv \
        --db "mysql+pymysql://user:pwd@host:9030/dwd"

    # 双人一致性检查
    python validate_golden.py --input golden_a.csv --file-b golden_b.csv

    # 输出 JSON 报告
    python validate_golden.py --input golden_set_v1.csv --report reports/eval_001.json

退出码: 0=通过（无 error）  1=存在 error，需修复后入库
"""

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict

# ────────────────────────────────────────────────────────────
# 常量定义
# ────────────────────────────────────────────────────────────
REQUIRED_FIELDS = [
    "id", "question", "role", "intent", "requires_clarify",
    "metrics", "date_basis", "dept_basis", "dedup",
    "permission_expect", "source", "difficulty",
]

# status 为流程字段，缺失时按 draft 处理
ENUMS = {
    "intent": ["query_metric", "compare", "rank", "trend", "attribution",
               "meta_question", "out_of_scope", "need_clarify"],
    "role": ["院长", "副院长", "医务处", "运营管理部", "财务处", "医保办",
             "科室主任", "医生", "系统管理员"],
    "date_basis": ["admit", "discharge", "settle", "visit", "operation", ""],
    # 门诊域多用 order/exec/belong；住院域多用 admit/discharge/belong
    "dept_basis": ["order", "exec", "belong", "admit", "discharge", ""],
    "dedup": ["visits", "patients", ""],
    "permission_expect": ["allow", "deny", "partial"],
    "difficulty": ["easy", "medium", "hard"],
    "source": ["meeting", "report", "interview", "ticket", "synthetic"],
    "status": ["draft", "reviewed", "adjudicated", "rejected"],
    "time_granularity": ["day", "week", "month", "quarter", "year", ""],
}

# 这些意图/权限下不需要 expected_sql
NO_SQL_INTENTS = {"meta_question", "out_of_scope"}

# ratio 指标（禁止用 AVG 计算）
RATIO_HINT_METRICS = {
    "ip_los_avg", "op_avg_fee", "ip_avg_fee", "drug_rate", "material_rate",
    "surgery_level4_rate", "cmi", "unplanned_readmit_31d_rate",
    "inpatient_death_rate", "bed_usage_rate",
}

# 覆盖要求
COVERAGE_RULES = [
    ("歧义题占比", lambda rows: _ratio(_count(rows, lambda r: _b(r.get("requires_clarify"))),
                                       len(rows)), 0.20, ">="),
    ("越权题占比", lambda rows: _ratio(_count(rows, lambda r: r.get("permission_expect") == "deny"),
                                       len(rows)), 0.05, ">="),
    ("超范围题占比", lambda rows: _ratio(_count(rows, lambda r: r.get("intent") == "out_of_scope"),
                                         len(rows)), 0.05, ">="),
    ("问口径题占比", lambda rows: _ratio(_count(rows, lambda r: r.get("intent") == "meta_question"),
                                         len(rows)), 0.05, ">="),
    ("synthetic 占比", lambda rows: _ratio(_count(rows, lambda r: r.get("source") == "synthetic"),
                                           len(rows)), 0.15, "<="),
]

MIN_METRIC_CASES = 8
MIN_RATIO_METRIC_CASES = 12
MIN_TOTAL = 300


def _b(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def _ratio(a, b):
    return a / b if b else 0.0


def _count(rows, pred):
    return sum(1 for r in rows if pred(r))


# 涉及"未结算/未落定"数据的时间范围
CURRENT_PERIOD_MARKERS = (
    "current_month", "current_quarter", "current_week", "year_to_date",
    "本月", "当月", "本周", "今天", "今年以来", "年内",
)


def _is_current_period(r):
    blob = f"{r.get('time_range') or ''} {r.get('time_expr') or ''}".lower()
    return any(m.lower() in blob for m in CURRENT_PERIOD_MARKERS)


# ────────────────────────────────────────────────────────────
# 加载
# ────────────────────────────────────────────────────────────
def load_rows(path):
    ext = os.path.splitext(path)[1].lower()
    rows = []
    if ext == ".jsonl":
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as e:
                    print(f"[FATAL] {path}:{i} JSON 解析失败: {e}")
                    sys.exit(1)
    else:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                rows.append(r)
    return rows


def parse_list_field(v):
    """CSV 中的 JSON 列表字段 → python list；空则 []"""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    s = str(v).strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
        return parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        return [x.strip() for x in s.split(",") if x.strip()]


def load_registry(path):
    try:
        import yaml  # type: ignore
    except ImportError:
        print("[WARN] 未安装 PyYAML，跳过注册表校验（pip install pyyaml）")
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    codes = set()
    for m in data.get("metrics", []) or []:
        codes.add(m.get("code"))
    return codes


def load_ambiguity(path):
    """歧义词表 → {rule_id: {"options": [...], "default": ..., "appliable": {...}}}

    ★ 存在的意义（docs/03 §6.1 / docs/13 §7 C9）：澄清后要让口径真的写回 DSL，
      语义层是按**字面**匹配选项的。标注时自己编一个措辞，管线会判成
      "未声明映射"而拒绝出数 —— 这类错误必须在入库前拦住，不能等评测时才暴露。
    """
    try:
        import yaml  # type: ignore
    except ImportError:
        print("[WARN] 未安装 PyYAML，跳过歧义词表校验（pip install pyyaml）")
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out = {}
    for r in data.get("rules", []) or []:
        ap = r.get("apply") or {}
        appliable = (set((ap.get("values") or {}).keys())
                     | set((ap.get("set") or {}).keys())
                     | set(ap.get("alias_to_default") or []))
        if r.get("default"):
            appliable.add(str(r["default"]))
        out[r.get("id")] = {"options": list(r.get("options") or []),
                            "default": r.get("default"),
                            "appliable": appliable}
    return out


# ────────────────────────────────────────────────────────────
# 校验
# ────────────────────────────────────────────────────────────
def validate(rows, registry_codes=None, db_url=None, ambiguity=None, verbose=True):
    errors, warnings = [], []

    def err(rid, msg):
        errors.append(f"[ERROR] {rid}: {msg}")

    def warn(rid, msg):
        warnings.append(f"[WARN ] {rid}: {msg}")

    seen_ids = set()
    metric_counter = Counter()

    for idx, r in enumerate(rows):
        rid = r.get("id") or f"<第{idx + 1}行>"

        # 1. 必填字段
        for f in REQUIRED_FIELDS:
            if f not in r or (isinstance(r[f], str) and not r[f].strip()):
                err(rid, f"必填字段缺失: {f}")

        # 2. id 唯一
        if rid in seen_ids:
            err(rid, "id 重复")
        seen_ids.add(rid)

        # 3. 枚举值
        for field, allowed in ENUMS.items():
            val = r.get(field)
            if val is None:
                continue
            val = str(val).strip()
            if val and val not in allowed:
                err(rid, f"{field} 取值非法: '{val}'（允许 {allowed}）")

        # 4. metrics
        metrics = parse_list_field(r.get("metrics"))
        for m in metrics:
            metric_counter[m] += 1
            if registry_codes is not None and m not in registry_codes:
                warn(rid, f"metric_code '{m}' 不在语义层注册表中")

        # 5. 歧义题必须有 rule_id
        if _b(r.get("requires_clarify")):
            rule_id = str(r.get("clarify_rule_id") or "").strip()
            resolution = str(r.get("clarify_resolution") or "").strip()
            if not rule_id:
                err(rid, "requires_clarify=true 但缺少 clarify_rule_id")
            if not resolution:
                warn(rid, "歧义题建议填写 clarify_resolution（澄清后的默认假设）")
            # 5.1 ★ 澄清取值必须能被语义层写回（docs/03 §6.1）
            #     写错措辞的后果不是"猜错"，而是管线拒绝出数（宁可说不会），
            #     在评测里表现为"这条题永远不会出数" —— 必须入库前拦住。
            if ambiguity is not None and rule_id:
                rule = ambiguity.get(rule_id)
                if rule is None:
                    err(rid, f"clarify_rule_id '{rule_id}' 不在歧义词表中")
                elif resolution and resolution not in rule["appliable"]:
                    err(rid, (f"clarify_resolution「{resolution}」无法被语义层写回，"
                              f"请改用 {rule_id} 的 options 原样文本"
                              f"（{' / '.join(rule['options'])}）"
                              f"或与 default 完全一致，"
                              f"否则管线会拒绝出数"))

        # 6. expected_sql
        sql = (r.get("expected_sql") or "").strip()
        intent = r.get("intent")
        perm = r.get("permission_expect")

        if perm == "deny" and sql:
            err(rid, "越权题（permission_expect=deny）不应提供 expected_sql")

        if intent not in NO_SQL_INTENTS and perm != "deny":
            if not sql:
                err(rid, "缺少 expected_sql（必须真实执行过）")
            else:
                if re.search(r"SELECT\s+\*", sql, re.I):
                    err(rid, "expected_sql 禁止使用 SELECT *")
                if re.search(r"\bBETWEEN\b.+\bAND\b", sql, re.I):
                    warn(rid, "建议用半开区间 [start, end) 代替 BETWEEN，避免边界问题")
                if any(m in RATIO_HINT_METRICS for m in metrics):
                    if re.search(r"\bAVG\s*\(", sql, re.I) and "NULLIF" not in sql.upper():
                        err(rid, "ratio 指标禁止 AVG 聚合，应为 SUM(num)/NULLIF(SUM(den),0)")
                if "NULLIF" not in sql.upper() and re.search(r"/\s*(COUNT|SUM)\s*\(", sql, re.I):
                    warn(rid, "除法建议加 NULLIF(...,0) 防止除零")
                if (_is_current_period(r)
                        and "data_maturity" not in sql
                        and "provisional" not in str(r.get("notes") or "").lower()):
                    warn(rid, "当月/当季数据需标注数据成熟度：SQL 加 data_maturity 过滤，"
                              "或在 notes 注明 provisional（前端应灰显）")
                has_date_filter = (re.search(r"(>=|<)\s*'", sql)
                                   or re.search(r"DATE_SUB|DATE_ADD|CURRENT_DATE|CURDATE", sql, re.I))
                if not has_date_filter:
                    warn(rid, "expected_sql 未检测到日期过滤条件")

    # 7. 覆盖分布
    total = len(rows)
    # 小样本（如样例集）时，覆盖类要求降级为 INFO，不做强制
    strict = total >= MIN_TOTAL
    infos = []

    def note(msg):
        prefix = "[WARN ]" if strict else "[INFO ]"
        (warnings if strict else infos).append(f"{prefix} {msg}")

    if not strict:
        infos.append(f"[INFO ] 样本量 {total} < {MIN_TOTAL}，"
                     f"覆盖类要求暂不校验（正式入库集需满足）")

    for name, fn, threshold, op in COVERAGE_RULES:
        val = fn(rows)
        flag = f"{val:.1%}"
        if op == ">=" and val < threshold:
            note(f"{name} {flag} < 要求 {threshold:.0%}")
        elif op == "<=" and val > threshold:
            note(f"{name} {flag} > 要求 {threshold:.0%}")

    # 8. 指标覆盖
    for m, cnt in sorted(metric_counter.items()):
        need = MIN_RATIO_METRIC_CASES if m in RATIO_HINT_METRICS else MIN_METRIC_CASES
        if cnt < need:
            note(f"指标 '{m}' 仅 {cnt} 条，建议 ≥ {need} 条")

    # 9. 连库执行校验
    sql_results = None
    if db_url:
        sql_results = _execute_sql(rows, db_url, errors, warn)

    return errors, warnings, infos, {
        "total": total,
        "metric_distribution": dict(metric_counter),
        "intent_distribution": dict(Counter(str(r.get("intent")) for r in rows)),
        "difficulty_distribution": dict(Counter(str(r.get("difficulty")) for r in rows)),
        "source_distribution": dict(Counter(str(r.get("source")) for r in rows)),
        "permission_distribution": dict(Counter(str(r.get("permission_expect")) for r in rows)),
        "clarify_count": _count(rows, lambda r: _b(r.get("requires_clarify"))),
    }


def _execute_sql(rows, db_url, errors, warn):
    try:
        import sqlalchemy  # type: ignore
    except ImportError:
        print("[WARN] 未安装 sqlalchemy，跳过 SQL 执行校验（pip install sqlalchemy）")
        return None

    from sqlalchemy import create_engine, text  # type: ignore
    engine = create_engine(db_url, pool_pre_ping=True)
    ok, empty, failed = 0, 0, 0
    with engine.connect() as conn:
        for r in rows:
            sql = (r.get("expected_sql") or "").strip()
            if not sql:
                continue
            rid = r.get("id")
            try:
                res = conn.execute(text(sql)).fetchall()
                if len(res) == 0:
                    empty += 1
                    warn(rid, "expected_sql 执行结果为空（无法判定正确性）")
                else:
                    ok += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                errors.append(f"[ERROR] {rid}: expected_sql 执行失败: {str(e)[:160]}")
    print(f"[INFO ] SQL 执行: 成功 {ok} / 空结果 {empty} / 失败 {failed}")
    return {"ok": ok, "empty": empty, "failed": failed}


# ────────────────────────────────────────────────────────────
# 双人一致性
# ────────────────────────────────────────────────────────────
def agreement(a_rows, b_rows):
    a = {r.get("id"): r for r in a_rows}
    b = {r.get("id"): r for r in b_rows}
    common = set(a) & set(b)
    if not common:
        return None

    def same_metrics(x, y):
        return set(parse_list_field(x.get("metrics"))) == set(parse_list_field(y.get("metrics")))

    metrics_agree = sum(1 for i in common if same_metrics(a[i], b[i]))
    intent_agree = sum(1 for i in common
                       if str(a[i].get("intent")).strip() == str(b[i].get("intent")).strip())
    clarify_agree = sum(1 for i in common
                        if _b(a[i].get("requires_clarify")) == _b(b[i].get("requires_clarify")))

    # Cohen's Kappa（按 intent）
    cats = sorted({str(a[i].get("intent")).strip() for i in common} |
                  {str(b[i].get("intent")).strip() for i in common})
    n = len(common)
    po = intent_agree / n
    pe = sum((sum(1 for i in common if str(a[i].get("intent")).strip() == c) / n) *
             (sum(1 for i in common if str(b[i].get("intent")).strip() == c) / n)
             for c in cats)
    kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0

    return {
        "common_ids": len(common),
        "metrics_agreement": round(metrics_agree / n, 4),
        "intent_agreement": round(intent_agree / n, 4),
        "clarify_agreement": round(clarify_agree / n, 4),
        "cohens_kappa_intent": round(kappa, 4),
        "metrics_pass": metrics_agree / n >= 0.85,
        "intent_pass": intent_agree / n >= 0.90,
        "kappa_pass": kappa >= 0.80,
    }


# ────────────────────────────────────────────────────────────
# main
# ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Golden 集标注质量校验")
    ap.add_argument("--input", required=True, help="Golden 集文件（.csv 或 .jsonl）")
    ap.add_argument("--file-b", help="第二位标注人的文件，用于一致性检查")
    ap.add_argument("--registry", help="语义层注册表 metric_registry.yaml")
    ap.add_argument("--ambiguity", help="歧义词表 ambiguity_rules.yaml"
                                        "（校验 clarify_resolution 能否被 apply 写回）")
    ap.add_argument("--db", help="数据库连接串，用于执行 expected_sql")
    ap.add_argument("--report", help="输出 JSON 报告路径")
    args = ap.parse_args()

    rows = load_rows(args.input)
    print(f"[INFO ] 加载 {len(rows)} 条: {args.input}")

    registry = load_registry(args.registry) if args.registry else None
    if registry:
        print(f"[INFO ] 注册表指标 {len(registry)} 个: {args.registry}")

    ambiguity = load_ambiguity(args.ambiguity) if args.ambiguity else None
    if ambiguity:
        print(f"[INFO ] 歧义规则 {len(ambiguity)} 条: {args.ambiguity}")

    errors, warnings, infos, stats = validate(rows, registry, args.db, ambiguity)

    print("\n──────── 分布统计 ────────")
    print(f"总数           : {stats['total']}")
    print(f"歧义题         : {stats['clarify_count']} "
          f"({_ratio(stats['clarify_count'], stats['total']):.1%})")
    print(f"意图分布       : {stats['intent_distribution']}")
    print(f"难度分布       : {stats['difficulty_distribution']}")
    print(f"来源分布       : {stats['source_distribution']}")
    print(f"权限分布       : {stats['permission_distribution']}")
    print(f"指标覆盖       : {len(stats['metric_distribution'])} 个指标")

    agr = None
    if args.file_b:
        agr = agreement(rows, load_rows(args.file_b))
        if agr is None:
            print("\n[ERROR] 两份文件无共同 id，无法计算一致性")
        else:
            print("\n──────── 双人一致性 ────────")
            for k in ("common_ids", "metrics_agreement", "intent_agreement",
                      "clarify_agreement", "cohens_kappa_intent"):
                print(f"{k:22s}: {agr[k]}")
            print(f"{'metrics_pass':22s}: {agr['metrics_pass']}  (要求 ≥ 0.85)")
            print(f"{'intent_pass':22s}: {agr['intent_pass']}  (要求 ≥ 0.90)")
            print(f"{'kappa_pass':22s}: {agr['kappa_pass']}  (要求 ≥ 0.80)")
            if not (agr["metrics_pass"] and agr["intent_pass"] and agr["kappa_pass"]):
                errors.append("[ERROR] 双人一致性未达门槛，需重新对齐规范后重标")

    print("\n──────── 提示信息 ────────")
    for i in infos:
        print(i)

    print("\n──────── 问题清单 ────────")
    for w in warnings:
        print(w)
    for e in errors:
        print(e)
    print(f"\n合计: {len(errors)} error / {len(warnings)} warning / {len(infos)} info")

    if args.report:
        os.makedirs(os.path.dirname(args.report) or ".", exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"input": args.input, "stats": stats, "agreement": agr,
                       "errors": errors, "warnings": warnings, "infos": infos},
                      f, ensure_ascii=False, indent=2)
        print(f"[INFO ] 报告已写入 {args.report}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
