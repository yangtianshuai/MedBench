#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""POC-B 消融对比：证明「语义层路线」到底带来多少增益。

对比组（与 docs/07 §B.3 对齐）：
    G0  裸 Text-to-SQL：只给表结构，不给语义层 → 证明"为什么不能裸跑 SQL"
    G1  语义层 + 受限 DSL（关掉歧义反问与意图规则预判）
    G2  G1 + Few-shot 示例库（semantic-layer/examples.yaml，动态选 3~5 条）
    G3  G1 + 歧义反问 + 意图规则预判（候选主方案）

用法：
    $env:ASK_LLM_API_KEY = "sk-xxx"
    python eval/run_ablation.py \
        --golden   eval/golden_set_sample.jsonl \
        --registry semantic-layer/metric_registry.yaml \
        --model    "deepseek-chat@https://api.deepseek.com" \
        --groups   g0,g1,g2,g3 \
        --output   eval/reports/ablation.json \
        --md       eval/reports/ablation.md

多模型对比（同一套用例 + 同一配置跑多个模型）：
    --model "deepseek-chat@https://api.deepseek.com" --model "qwen2.5-14b@http://10.0.0.9:8000/v1"

★ 两条硬约束：
  1) G0 只生成 SQL 文本，**绝不执行**（无库，也不允许连库跑）。
  2) G0 的判定是「口径要素命中率」启发式检查，不是结果集比对，
     报告中必须显著标注，不得与 G1/G3 的正确率混为一谈。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, os.path.join(_ROOT, "engine"))
sys.path.insert(0, _HERE)

from pipeline import (AskPipeline, HTTPLLM, load_yaml,  # noqa: E402
                      build_schema_digest)
from run_golden import evaluate  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None


# ────────────────────────────────────────────────────────────
# G0：裸 Text-to-SQL
# ────────────────────────────────────────────────────────────
G0_PROMPT = """你是资深 SQL 工程师，请根据表结构写出能回答问题的 SQL（MySQL 方言）。

【表结构】
{schema}

【要求】
1. 只输出 SQL，不要解释，不要 markdown 代码块标记。
2. 必须写全口径：日期字段、科室归属字段、去重方式都要显式写出来。
3. 时间范围用具体日期区间（基准日期：{today}）。

【问题】{question}
"""


class G0Engine:
    """纯 Text-to-SQL 基线：无语义层、无权限层、无口径回显。

    ★ 故意不加任何校验 —— 这就是要量化的「裸跑风险」。
    """

    name = "g0_text_to_sql"

    def __init__(self, registry: Dict[str, Any], llm, today: dt.date):
        self.registry = registry
        self.llm = llm
        self.today = today
        self.schema = build_schema_digest(registry)
        self.records: List[Dict[str, Any]] = []

    def query(self, case: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        q = case.get("question", "")
        prompt = G0_PROMPT.format(schema=self.schema, today=self.today, question=q)
        t0 = time.time()
        try:
            out = self.llm.generate_raw(prompt)
        except Exception as e:  # noqa: BLE001
            out = f"-- ERROR: {e}"
        ms = int((time.time() - t0) * 1000)
        sql = _strip_markdown(out)
        self.records.append({"id": case.get("id"), "sql": sql, "ms": ms})
        return {"dsl": {"intent": "error"}, "latency_ms": ms,
                "status": "answered", "sql": sql, "answer": sql}


def _strip_markdown(text: str) -> str:
    t = (text or "").strip()
    t = re.sub(r"^```(?:sql)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    return t


# ────────────────────────────────────────────────────────────
# G0 启发式判定：SQL 是否写全了口径要素
# ────────────────────────────────────────────────────────────
def _index(rows) -> Dict[str, Dict[str, Any]]:
    return {r["code"]: r for r in (rows or [])}


def _measure_of(registry: Dict[str, Any], metric_code: str):
    m = _index(registry.get("metrics")).get(metric_code)
    if not m:
        return None, None
    measures = _index(registry.get("measures"))
    if m.get("type") == "ratio":
        num = m.get("numerator")
        den = m.get("denominator")
        n = num[0] if isinstance(num, list) else num
        d = den[0] if isinstance(den, list) else den
        return measures.get(n), measures.get(d)
    return measures.get(m.get("measure")), None


def required_tokens(case: Dict[str, Any], registry: Dict[str, Any]) -> Dict[str, Any]:
    """从 golden 推出「SQL 必须命中哪些口径要素」。

    ★ 这是启发式：命中不等于数对（未连库），但**漏了几乎必然错**
      —— 漏掉日期口径字段、漏掉去重、漏掉除零保护，都是临床上会挨骂的错误。
    """
    req: Dict[str, Any] = {"table": [], "expr": [], "date_col": [], "dept_col": [],
                           "dedup": None, "ratio_nullif": False, "time_filter": True}
    codes = case.get("metrics") or []
    for code in codes:
        mnum, mden = _measure_of(registry, code)
        for mm in (mnum, mden):
            if not mm:
                continue
            if mm.get("table") and mm["table"] not in req["table"]:
                req["table"].append(mm["table"])
            if mm.get("expr") and mm["expr"] not in req["expr"]:
                req["expr"].append(mm["expr"])
        if _index(registry.get("metrics")).get(code, {}).get("type") == "ratio":
            req["ratio_nullif"] = True
        # 日期 / 科室口径字段
        ref = _index(registry.get("metrics")).get(code) or {}
        db = case.get("date_basis") or ref.get("default_date_basis")
        emap = (mnum or {}).get("date_basis", {}) or {}
        col = (emap.get("expr_map") or {}).get(db)
        if col and col not in req["date_col"]:
            req["date_col"].append(col)
        ob = case.get("dept_basis") or ref.get("default_dept_basis")
        demap = (mnum or {}).get("dept_basis", {}) or {}
        dcol = (demap.get("expr_map") or {}).get(ob)
        if dcol and dcol not in req["dept_col"]:
            req["dept_col"].append(dcol)

    dims = set(case.get("dimensions") or [])
    has_dept = bool(dims & {"dept", "campus"}) or any(
        f.get("field") in ("dept", "campus") for f in (case.get("filters") or []))
    dedup = case.get("dedup") or "visits"
    return {"req": req, "has_dept": has_dept, "dedup": dedup}


def check_sql(sql: str, spec: Dict[str, Any]) -> Dict[str, bool]:
    s = re.sub(r"\s+", " ", (sql or "")).lower()
    req = spec["req"]
    hit = {
        "table": any(t.lower() in s for t in req["table"]) if req["table"] else True,
        "expr": any(e.lower() in s for e in req["expr"]) if req["expr"] else True,
        "date_col": any(c.lower() in s for c in req["date_col"]) if req["date_col"] else True,
        "time_filter": bool(re.search(r"(between|>=|<=|>|<)\s*'?\d{4}-\d{2}-\d{2}", s)),
        "ratio_nullif": (not req["ratio_nullif"]) or ("nullif" in s),
    }
    if spec["has_dept"]:
        hit["dept_col"] = any(c.lower() in s for c in req["dept_col"]) \
            if req["dept_col"] else True
    if spec["dedup"] == "patients":
        hit["dedup"] = "distinct" in s and "patient" in s
    return hit


# ────────────────────────────────────────────────────────────
# G1 / G3：语义层管线
# ────────────────────────────────────────────────────────────
_STATUS_TO_DSL = {
    "need_clarify": {"intent": "need_clarify"},
    "out_of_scope": {"intent": "out_of_scope"},
    "meta": {"intent": "meta_question"},
    "denied": {"intent": "query_metric"},      # 判定看 clear_intent，不比 DSL
    "error": {"intent": "error"},
}


class PipelineEngine:
    def __init__(self, registry, ambiguity, llm, today, group: str):
        self.name = {"g1": "g1_semantic_layer", "g2": "g2_plus_fewshot",
                     "g3": "g3_full"}[group]
        self.pipe = AskPipeline(
            registry, ambiguity, llm, None, today,
            enable_ambiguity=(group == "g3"),
            enable_intent_hint=(group == "g3"),
            # ★ 只给 G2 开示例：G1/G3 必须与历史基线逐字可比，否则消融结论不成立
            enable_fewshot=(group == "g2"))
        self.records: List[Dict[str, Any]] = []

    def query(self, case, ctx):
        t0 = time.time()
        r = self.pipe.ask(
            case.get("question", ""),
            role=ctx.get("role") or case.get("role") or "guest",
            dept_scope=ctx.get("dept_scope") or case.get("dept_scope") or ["*"])
        ms = int((time.time() - t0) * 1000)
        dsl = _STATUS_TO_DSL.get(r.status)
        if dsl is None:
            dsl = r.raw_dsl or {"intent": "error"}
        self.records.append({"id": case.get("id"), "status": r.status,
                             "raw_dsl": r.raw_dsl, "sql": r.sql, "ms": ms})
        return {"dsl": dsl, "latency_ms": ms, "status": r.status,
                "sql": r.sql, "answer": r.answer, "warnings": r.warnings}


def _ctx_of(case: Dict[str, Any]) -> Dict[str, Any]:
    """★ 角色与数据范围必须来自 Golden 集本身（与 run_golden.evaluate 一致）。
       自行猜测 scope 会让越权用例的判定失真（实测漏放 0 → 2 就是这么来的）。
    """
    return {"role": case.get("role"), "dept_scope": case.get("dept_scope")}


def pct(x: int, n: int) -> float:
    return round(100.0 * x / n, 1) if n else 0.0


def run_group(group: str, golden, engine, registry, today) -> Dict[str, Any]:
    if group == "g0":
        return _run_g0(golden, engine, registry)
    report = evaluate(golden, engine, registry, None, today)
    s = report["summary"]
    # evaluate 的 summary 存的是 0~1 比例，这里统一换算成百分数
    pc = lambda k: round(float(s[k]) * 100, 1)  # noqa: E731
    return {"group": group, "engine": engine.name, "mode": "dsl_equivalent",
            "intent_accuracy": pc("intent_accuracy"),
            "dsl_executable_rate": pc("dsl_executable_rate"),
            "result_correctness": pc("result_correctness"),
            "clarify_quality": pc("clarify_quality"),
            "false_ask_rate": pc("false_ask_rate"),
            "reject_correctness": pc("reject_correctness"),
            "leak_count": s["leak_count"],
            "leak_ids": [r["id"] for r in report["rows"]
                         if r.get("restricted") and not r.get("rejected")],
            "p50_ms": s.get("p50_latency_ms", 0), "p95_ms": s.get("p95_latency_ms", 0),
            "failed": [{"id": r["id"], "gold": r["gold_intent"],
                        "pred": r["pred_intent"]}
                       for r in report["rows"] if not r["correct"]]}


def _run_g0(golden, engine: G0Engine, registry) -> Dict[str, Any]:
    """G0 单独统计：口径要素命中率 + 越权/超范围是否被拦。"""
    hits: Dict[str, int] = {}
    total = 0
    all_hit = 0
    denied_total = 0
    # ★ G0 不走 evaluate（它产出的是 SQL 文本不是 DSL），必须自己驱动引擎
    for case in golden:
        engine.query(case, _ctx_of(case))
    for case, rec in zip(golden, engine.records):
        spec = required_tokens(case, registry)
        hit = check_sql(rec["sql"], spec)
        total += 1
        for k, v in hit.items():
            hits[k] = hits.get(k, 0) + (1 if v else 0)
        if all(hit.values()):
            all_hit += 1
        # G0 没有权限层：越权题只要生成了 SQL 就是「未拦截」（这正是要展示的风险）
        if (case.get("permission_expect") or "allow") != "allow":
            denied_total += 1
    return {
        "group": "g0", "engine": engine.name,
        "mode": "sql_heuristic（启发式，非结果比对）",
        "n": total,
        "element_hit_rate": {k: pct(v, total) for k, v in hits.items()},
        "all_elements_hit_rate": pct(all_hit, total),
        # G0 无权限层：越权题一律不会被拦（这是设计使然，正是要展示的风险）
        "reject_correctness": 0.0 if denied_total else None,
        "leak_count": denied_total,
        "p50_ms": _q([r["ms"] for r in engine.records], 0.5),
        "p95_ms": _q([r["ms"] for r in engine.records], 0.95),
        "note": f"越权/超范围题 {denied_total} 条全部未被拦截（G0 无权限层）",
    }


def _q(xs: List[int], q: float) -> int:
    if not xs:
        return 0
    s = sorted(xs)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


# ────────────────────────────────────────────────────────────
# 报告
# ────────────────────────────────────────────────────────────
def write_md(path: str, results: List[Dict[str, Any]], golden_n: int,
             today: str) -> None:
    L = ["# POC-B 消融对比报告", "",
         f"- 用例数：{golden_n}　基线日期：{today}",
         "- 判定方式：G1/G2/G3 = DSL 等价；**G0 = SQL 口径要素启发式检查（非结果比对）**",
         "",
         "## 1. 组间对比", "",
         "| 组 | 配置 | 结果正确率 | 意图准确率 | DSL 可执行率 | 反问得当率 | 越权拒绝 | 漏放 | P95 |",
         "|---|---|---|---|---|---|---|---|---|"]
    label = {"g0": "裸 Text-to-SQL（无语义层）", "g1": "语义层 + 受限 DSL",
             "g2": "G1 + Few-shot", "g3": "G1 + 反问 + 规则预判（候选主方案）"}
    for r in results:
        if r["group"] == "g0":
            L.append(f"| G0 | {label['g0']} | {r['all_elements_hit_rate']}%* | — | — | — | "
                     f"0% | **{r['leak_count']}** | {r['p95_ms']}ms |")
            continue
        L.append(f"| {r['group'].upper()} | {label[r['group']]} | "
                 f"{r['result_correctness']}% | {r['intent_accuracy']}% | "
                 f"{r['dsl_executable_rate']}% | {r['clarify_quality']}% | "
                 f"{r['reject_correctness']}% | {r['leak_count']} | {r['p95_ms']}ms |")
    L += ["", "> \\* G0 的正确率是「SQL 口径要素全部命中」的比例，"
              "属于启发式上界：命中不等于数对，漏了几乎必然错。", ""]

    g0 = next((r for r in results if r["group"] == "g0"), None)
    if g0:
        L += ["## 2. G0 裸跑 SQL 的口径要素命中率", "",
              "| 口径要素 | 命中率 | 漏掉的后果 |", "|---|---|---|"]
        why = {"table": "查错表，数字张冠李戴",
               "expr": "用错字段（如把人次写成人数）",
               "date_col": "日期口径错（出院日 vs 结算日），月度数对不上",
               "time_filter": "没有时间范围 → 全表扫描 / 口径不明",
               "dept_col": "科室归属错（开单科室 vs 出院科室）",
               "dedup": "未去重 → 人次被重复计数",
               "ratio_nullif": "除零 → 报错或脏数"}
        for k, v in sorted(g0["element_hit_rate"].items()):
            L.append(f"| {k} | {v}% | {why.get(k, '')} |")
        L += ["", f"**全部要素命中：{g0['all_elements_hit_rate']}%**　"
                  f"（{g0.get('note','')}）", ""]

    g3 = next((r for r in results if r["group"] == "g3"), None)
    g1 = next((r for r in results if r["group"] == "g1"), None)
    if g3 and g1:
        L += ["## 3. 语义层路线的增益", "",
              "| 对比 | 增益 |", "|---|---|",
              f"| G3 − G1（反问机制 + 规则预判） | "
              f"{round(g3['result_correctness'] - g1['result_correctness'], 1)} pt |"]
        if g0:
            L.append(f"| G3 − G0（语义层 vs 裸跑 SQL） | "
                     f"{round(g3['result_correctness'] - g0['all_elements_hit_rate'], 1)} pt* |")
        L += ["", "> 门槛：G3 vs G0 增益 ≥ +15 pt（docs/07 §B.5）", ""]

    for r in results:
        if r.get("failed"):
            L += [f"## G{r['group'][1:].upper()} 失败用例（{len(r['failed'])} 条）", "",
                  "| 用例 | 期望 | 实际 |", "|---|---|---|"]
            L += [f"| {f['id']} | {f['gold']} | {f['pred']} |" for f in r["failed"]]
            L.append("")
    L.append("> ⚠️ G0 只生成 SQL 文本，未连接数据库、未执行任何语句。")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main() -> int:
    ap = argparse.ArgumentParser(description="POC-B 消融对比（G0/G1/G3 × 多模型）")
    ap.add_argument("--golden", default=os.path.join(_HERE, "golden_set_sample.jsonl"))
    ap.add_argument("--registry", default=os.path.join(
        _ROOT, "semantic-layer", "metric_registry.yaml"))
    ap.add_argument("--ambiguity", default=os.path.join(
        _ROOT, "semantic-layer", "ambiguity_rules.yaml"))
    ap.add_argument("--model", action="append", required=True,
                    help="模型，格式 名称@base_url，可多次传入以做多模型对比")
    ap.add_argument("--groups", default="g0,g1,g2,g3")
    ap.add_argument("--api-key-env", default="ASK_LLM_API_KEY")
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--ids", help="只跑指定用例（逗号分隔），调试用")
    ap.add_argument("--output", help="JSON 报告路径")
    ap.add_argument("--md", help="Markdown 报告路径")
    args = ap.parse_args()

    if yaml is None:
        print("[FATAL] 需要 pyyaml")
        return 2
    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print(f"[FATAL] 请设置环境变量 {args.api_key_env}（不要写进命令行/仓库）")
        return 2

    registry = load_yaml(args.registry)
    ambiguity = load_yaml(args.ambiguity)
    today = dt.date.fromisoformat(args.today)
    golden = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]
    if args.ids:
        want = set(args.ids.split(","))
        golden = [c for c in golden if c.get("id") in want]

    results: List[Dict[str, Any]] = []
    for spec in args.model:
        if "@" not in spec:
            print(f"[FATAL] --model 需形如 名称@base_url：{spec}")
            return 2
        model, base = spec.split("@", 1)
        llm = HTTPLLM(base_url=base, api_key=api_key, model=model,
                      timeout=args.timeout)
        for group in [g.strip().lower() for g in args.groups.split(",")]:
            print(f"\n=== {model} · {group.upper()} · {len(golden)} 条 ===")
            if group == "g0":
                if not hasattr(llm, "generate_raw"):
                    print("[SKIP] G0 需要 LLM 适配器支持 generate_raw")
                    continue
                eng = G0Engine(registry, llm, today)
            else:
                eng = PipelineEngine(registry, ambiguity, llm, today, group)
            r = run_group(group, golden, eng, registry, today)
            r["model"] = model
            results.append(r)
            if group == "g0":
                print(f"  全部要素命中 {r['all_elements_hit_rate']}%  "
                      f"要素：{r['element_hit_rate']}")
            else:
                print(f"  正确率 {r['result_correctness']}%  意图 {r['intent_accuracy']}%  "
                      f"可执行 {r['dsl_executable_rate']}%  漏放 {r['leak_count']}")

    print("\n" + "=" * 76)
    for r in results:
        g = r["group"].upper()
        if g == "G0":
            print(f"{r['model']:<16} {g}  要素全命中 {r['all_elements_hit_rate']:>5}%  "
                  f"越权未拦 {r['leak_count']} 条")
        else:
            print(f"{r['model']:<16} {g}  正确率 {r['result_correctness']:>5}%  "
                  f"漏放 {r['leak_count']}  P95 {r['p95_ms']}ms")

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"today": args.today, "n": len(golden), "results": results},
                      f, ensure_ascii=False, indent=2)
        print(f"[INFO] JSON 报告: {args.output}")
    if args.md:
        os.makedirs(os.path.dirname(args.md) or ".", exist_ok=True)
        write_md(args.md, results, len(golden), args.today)
        print(f"[INFO] Markdown 报告: {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
