#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""POC-B：真实 LLM + 真实管线 跑 Golden 集。

与 run_golden.py 的区别：
  - run_golden.py 的 mock 引擎直接返回【标注答案】，只用来验证「评估器」本身；
  - 本脚本让 LLM 真的去生成 DSL，管线（校验/权限/反问/SQL）全量真实执行，
    产出的是**模型 + 语义层的联合准确率**，这才是能拿去立项的数。

用法（API Key 只从环境变量取，禁止写进仓库）：
    $env:ASK_LLM_API_KEY = "sk-xxx"
    python eval/poc_b_real_llm.py --golden eval/golden_set_sample.jsonl \
        --registry semantic-layer/metric_registry.yaml \
        --base-url https://api.deepseek.com --model deepseek-chat

⚠️ 合规：医院正式环境必须指向院内网私有化部署的模型服务。
   本脚本只发送【问句 + 指标字典候选】，不发送任何患者数据；
   若用公网 API，务必确认问句中不含 PII。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
for p in (os.path.join(_ROOT, "engine"), _HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml  # noqa: E402

from pipeline import AskPipeline, HTTPLLM  # noqa: E402
from run_golden import BaseEngine, evaluate  # noqa: E402

# 管线 status → 评估器能理解的 DSL 形态
_STATUS_TO_DSL = {
    "answered": None,          # 用 raw_dsl 原样回传
    "need_clarify": {"intent": "need_clarify"},
    "out_of_scope": {"intent": "out_of_scope"},
    "denied": {"intent": "out_of_scope"},      # 越权：对外表现为拒绝出数
    "meta": {"intent": "meta_question"},
    "error": {"intent": "error"},
}


class PipelineEngine(BaseEngine):
    """把真实 AskPipeline 包装成评估器的 BaseEngine 接口。"""

    name = "real_pipeline"

    def __init__(self, registry, ambiguity, llm, today):
        self.registry = registry
        self.ambiguity = ambiguity
        self.llm = llm
        self.today = today
        self.records: list = []          # 与 golden 顺序一致，供 --verbose 逐条复盘

    def query(self, case, ctx):
        p = AskPipeline(self.registry, self.ambiguity, llm=self.llm, today=self.today)
        t0 = time.time()
        r = p.ask(case.get("question", ""),
                  role=case.get("role") or "guest",
                  dept_scope=case.get("dept_scope") or ["*"])
        ms = int((time.time() - t0) * 1000)
        dsl = _STATUS_TO_DSL.get(r.status, {"intent": "error"})
        if dsl is None:
            dsl = r.raw_dsl or {"intent": "error"}   # answered 必须有合法 DSL
        self.records.append({"id": case.get("id"), "question": case.get("question"),
                             "status": r.status, "raw_dsl": r.raw_dsl,
                             "sql": r.sql, "answer": r.answer, "ms": ms,
                             "trace": r.trace})
        return {"dsl": dsl, "latency_ms": ms,
                "status": r.status, "sql": r.sql, "answer": r.answer,
                "warnings": r.warnings}


def main():
    ap = argparse.ArgumentParser(description="POC-B：真实 LLM 跑 Golden 集")
    ap.add_argument("--golden", default=os.path.join(_HERE, "golden_set_sample.jsonl"))
    ap.add_argument("--registry", default=os.path.join(
        _ROOT, "semantic-layer", "metric_registry.yaml"))
    ap.add_argument("--rules", default=os.path.join(
        _ROOT, "semantic-layer", "ambiguity_rules.yaml"))
    ap.add_argument("--base-url", default=os.getenv("ASK_LLM_BASE_URL", ""))
    ap.add_argument("--model", default=os.getenv("ASK_LLM_MODEL", "deepseek-chat"))
    ap.add_argument("--api-key", default=os.getenv("ASK_LLM_API_KEY", ""),
                    help="★ 建议只通过环境变量 ASK_LLM_API_KEY 传入，不要写在命令行历史里")
    ap.add_argument("--today", default="2026-10-14")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 条（调试用）")
    ap.add_argument("--ids", help="只跑指定 id，逗号分隔")
    ap.add_argument("--output", help="输出 JSON 报告路径")
    ap.add_argument("--md", help="输出 Markdown 报告路径")
    ap.add_argument("--verbose", action="store_true",
                    help="逐条打印模型原始 DSL / SQL / 回炉次数（定位短板用）")
    args = ap.parse_args()

    if not args.base_url or not args.api_key:
        raise SystemExit("需要 --base-url 与 --api-key（或环境变量 ASK_LLM_BASE_URL / ASK_LLM_API_KEY）")

    golden = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]
    if args.ids:
        want = {s.strip() for s in args.ids.split(",")}
        golden = [c for c in golden if c.get("id") in want]
    if args.limit:
        golden = golden[:args.limit]

    registry = yaml.safe_load(open(args.registry, encoding="utf-8"))
    rules = yaml.safe_load(open(args.rules, encoding="utf-8"))
    today = dt.date.fromisoformat(args.today)

    llm = HTTPLLM(model=args.model, base_url=args.base_url, api_key=args.api_key)
    engine = PipelineEngine(registry, rules, llm, today)

    print(f"[POC-B] 模型={args.model}  端点={args.base_url}  用例={len(golden)}  基线日期={today}")
    print("=" * 76)
    for i, c in enumerate(golden, 1):
        print(f"  [{i}/{len(golden)}] {c['id']} {c['question'][:34]}", flush=True)
    print("=" * 76)

    report = evaluate(golden, engine, registry, None, args.today)
    s = report["summary"]
    s["model"] = args.model
    s["endpoint"] = args.base_url

    # 把模型原始输出并入报告（复盘时能看出「错在模型」还是「错在语义层/标注」）
    recs = {r["id"]: r for r in engine.records}
    for row in report["rows"]:
        rec = recs.get(row["id"]) or {}
        row["status"] = rec.get("status")
        row["llm_dsl"] = rec.get("raw_dsl")
        row["sql"] = rec.get("sql")
        row["repairs"] = sum(1 for t in (rec.get("trace") or [])
                             if t.get("step") == "repair")

    print(f"判定方式   : {s['judge_mode']}（无库，DSL 等价比对）")
    print(f"意图准确率      : {s['intent_accuracy']:.1%}")
    print(f"DSL 可执行率    : {s['dsl_executable_rate']:.1%}")
    print(f"结果正确率      : {s['result_correctness']:.1%}   ← 核心指标")
    print(f"反问得当率      : {s['clarify_quality']:.1%}")
    print(f"误反问率        : {s['false_ask_rate']:.1%}")
    print(f"越权拒绝正确率  : {s['reject_correctness']:.1%}   漏放 {s['leak_count']} 条")
    print(f"延迟 P50 / P95  : {s['p50_latency_ms']}ms / {s['p95_latency_ms']}ms")

    if args.verbose:
        print("=" * 76)
        for rec, row in zip(engine.records, report["rows"]):
            repairs = sum(1 for t in rec["trace"] if t.get("step") == "repair")
            print(f"[{rec['id']}] {'OK ' if row['correct'] else 'BAD'} "
                  f"status={rec['status']} {rec['ms']}ms 回炉={repairs}")
            print(f"   Q: {rec['question']}")
            print(f"   DSL: {json.dumps(rec['raw_dsl'], ensure_ascii=False)}")
            if rec["sql"]:
                print(f"   SQL: {rec['sql'][:160]}")
            if rec["status"] == "error":
                print(f"   ERR: {str(rec['answer'])[:300]}")
            if not row["correct"]:
                print(f"   gold: intent={row['gold_intent']} "
                      f"pred={row['pred_intent']} exec={row['dsl_executable']}")
            print()

    failed = [r for r in report["rows"] if not r["correct"]]
    if failed:
        print("-" * 76)
        print(f"失败 {len(failed)} 条:")
        for r in failed:
            print(f"  {r['id']} gold={r['gold_intent']} pred={r['pred_intent']} "
                  f"| {str(r['question'])[:36]}")

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        json.dump(report, open(args.output, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"[INFO] JSON 报告: {args.output}")
    if args.md:
        os.makedirs(os.path.dirname(os.path.abspath(args.md)), exist_ok=True)
        _write_md(args.md, report, args)
        print(f"[INFO] Markdown 报告: {args.md}")
    return 0


def _write_md(path, report, args):
    s, rows = report["summary"], report["rows"]
    golden = {r["id"]: r for r in rows}
    L = [
        f"# POC-B 真实 LLM 评测报告",
        "",
        f"- 模型：`{args.model}`　端点：`{args.base_url}`",
        f"- 用例数：{s['total']}　基线日期：{args.today}",
        f"- 判定方式：**{s['judge_mode']}**（未连库，未做结果集比对）",
        "",
        "## 核心指标",
        "",
        "| 指标 | 结果 | 立项门槛 | 结论 |",
        "|---|---|---|---|",
        f"| 意图准确率 | {s['intent_accuracy']:.1%} | ≥90% | {'达标' if s['intent_accuracy'] >= .9 else '未达标'} |",
        f"| DSL 可执行率 | {s['dsl_executable_rate']:.1%} | ≥95% | {'达标' if s['dsl_executable_rate'] >= .95 else '未达标'} |",
        f"| 结果正确率 | {s['result_correctness']:.1%} | ≥90% | {'达标' if s['result_correctness'] >= .9 else '未达标'} |",
        f"| 反问得当率 | {s['clarify_quality']:.1%} | ≥95% | {'达标' if s['clarify_quality'] >= .95 else '未达标'} |",
        f"| 越权拒绝正确率 | {s['reject_correctness']:.1%} | 100%（一票否决） | {'达标' if s['reject_correctness'] == 1 else '未达标'} |",
        f"| 延迟 P50 / P95 | {s['p50_latency_ms']}ms / {s['p95_latency_ms']}ms | P95<5s | "
        f"{'达标' if s['p95_latency_ms'] < 5000 else '未达标'} |",
        "",
        "## 逐条明细",
        "",
        "| 用例 | 问题 | 期望 | 实际 | 正确 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        L.append(f"| {r['id']} | {str(r['question'])[:30]} | {r['gold_intent']} | "
                 f"{r['pred_intent']} | {'✅' if r['correct'] else '❌'} |")
    L += ["", "> ⚠️ 本轮未连真实数仓（判定为 DSL 等价），**结果正确率不等于口径正确率**；",
          "> 连库后须以 `expected_sql` 的执行结果为准再复跑一次。", ""]
    open(path, "w", encoding="utf-8").write("\n".join(L))


if __name__ == "__main__":
    sys.exit(main())
