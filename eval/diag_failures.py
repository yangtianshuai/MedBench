#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""失败诊断：区分「模型抖动」与「系统性错误」，并自动归因到字段级。

为什么要跑多次：31 条样本上单次运行波动可达 ±6pt（1 条 = 3.2pt），
单次结果既不能判定达标，也不能定位问题。本脚本对每条用例重复 N 次，
把失败分成两类：
    稳定失败（失败率 = 100%）→ 系统性缺陷，必须改代码/改提示词/改注册表
    抖动失败（0 < 失败率 < 100%）→ 模型不确定性，靠自洽投票或加约束解决

用法：
    $env:ASK_LLM_API_KEY = "sk-xxx"
    python eval/diag_failures.py --repeat 3 \
        --model "deepseek-chat@https://api.deepseek.com" \
        --md eval/reports/failure_diag.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, os.path.join(_ROOT, "engine"))
sys.path.insert(0, _HERE)

from pipeline import AskPipeline, HTTPLLM, load_yaml  # noqa: E402
from run_golden import evaluate  # noqa: E402


def _set(x) -> set:
    return set(x or [])


def _filters_key(fs) -> set:
    out = set()
    for f in (fs or []):
        if not isinstance(f, dict):
            continue
        v = f.get("value")
        out.add((f.get("field"), f.get("op"), json.dumps(v, ensure_ascii=False,
                                                        sort_keys=True)
                 if isinstance(v, (list, dict)) else v))
    return out


def diff_fields(case: Dict[str, Any], raw: Dict[str, Any], status: str,
                caliber: Optional[Dict[str, Any]]) -> List[str]:
    """把「为什么错」归因到具体字段（口径一律以 build() 回显的 caliber 为准）。"""
    if not raw:
        return ["无 DSL 输出"]
    why: List[str] = []
    gold_intent = case.get("intent")
    pred_intent = raw.get("intent")

    # 1) 意图 / 反问 / 拒绝
    if status == "need_clarify" and not case.get("requires_clarify"):
        why.append("误反问（不该问却问了）")
        return why
    if case.get("requires_clarify") and status != "need_clarify":
        why.append("该反问没反问")
    if gold_intent != pred_intent and not (
            case.get("requires_clarify") or gold_intent == "query_metric"
            and pred_intent in ("compare", "rank")):
        why.append(f"intent 错（期望 {gold_intent} / 实际 {pred_intent}）")

    # 2) 指标
    if _set(case.get("metrics")) != _set(raw.get("metrics")):
        why.append(f"指标错（期望 {case.get('metrics')} / 实际 {raw.get('metrics')}）")

    # 3) 维度 / 过滤
    if _set(case.get("dimensions")) != _set(raw.get("dimensions")):
        why.append(f"维度错（期望 {case.get('dimensions')} / 实际 {raw.get('dimensions')}）")
    if _filters_key(case.get("filters")) != _filters_key(raw.get("filters")):
        why.append(f"过滤错（期望 {case.get('filters')} / 实际 {raw.get('filters')}）")

    # 4) 时间
    t = raw.get("time") or {}
    g_range = case.get("time_range")
    g_range = g_range.get("type") if isinstance(g_range, dict) else g_range
    p_range = t.get("range")
    p_range = p_range.get("type") if isinstance(p_range, dict) else p_range
    if g_range and p_range and g_range != p_range:
        why.append(f"时间范围错（期望 {g_range} / 实际 {p_range}）")
    if (case.get("compare") or "none") != (t.get("compare") or "none"):
        why.append(f"对比方式错（期望 {case.get('compare')} / 实际 {t.get('compare')}）")

    # 5) 口径（用 caliber 回显，避免「省略即默认」被误判）
    if caliber:
        for key, gold in (("date_basis", case.get("date_basis")),
                          ("dept_basis", case.get("dept_basis")),
                          ("dedup", case.get("dedup"))):
            if gold and caliber.get(key) and gold != caliber.get(key):
                why.append(f"{key} 错（期望 {gold} / 实际 {caliber.get(key)}）")

    # 6) 排序 / TopN
    opt = raw.get("options") or {}
    if case.get("top_n") and opt.get("top_n") != case.get("top_n"):
        why.append(f"top_n 错（期望 {case.get('top_n')} / 实际 {opt.get('top_n')}）")
    if case.get("order_by") and _set([case["order_by"].get("metric")]) != \
            _set([(opt.get("order_by") or {}).get("metric")]):
        why.append("排序字段错")

    return why or ["判定不等价（字段级未定位到差异，需人工看 DSL）"]


def run_once(golden, registry, ambiguity, llm, today) -> Dict[str, Dict[str, Any]]:
    pipe = AskPipeline(registry, ambiguity, llm, None, today)
    out: Dict[str, Dict[str, Any]] = {}
    for case in golden:
        t0 = time.time()
        r = pipe.ask(case.get("question", ""),
                     role=case.get("role") or "guest",
                     dept_scope=case.get("dept_scope") or ["*"])
        ms = int((time.time() - t0) * 1000)
        out[case["id"]] = {"status": r.status, "raw": r.raw_dsl,
                           "caliber": r.caliber, "sql": r.sql, "ms": ms}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="失败诊断（多次运行 + 字段级归因）")
    ap.add_argument("--golden", default=os.path.join(_HERE, "golden_set_sample.jsonl"))
    ap.add_argument("--registry", default=os.path.join(
        _ROOT, "semantic-layer", "metric_registry.yaml"))
    ap.add_argument("--ambiguity", default=os.path.join(
        _ROOT, "semantic-layer", "ambiguity_rules.yaml"))
    ap.add_argument("--model", default="deepseek-chat@https://api.deepseek.com")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--today", default=dt.date.today().isoformat())
    ap.add_argument("--api-key-env", default="ASK_LLM_API_KEY")
    ap.add_argument("--md", help="输出 Markdown 报告")
    ap.add_argument("--output", help="输出 JSON")
    args = ap.parse_args()

    api_key = os.environ.get(args.api_key_env)
    if not api_key:
        print(f"[FATAL] 请设置环境变量 {args.api_key_env}")
        return 2
    model, base = args.model.split("@", 1)
    llm = HTTPLLM(base_url=base, api_key=api_key, model=model, timeout=args.timeout)
    registry = load_yaml(args.registry)
    ambiguity = load_yaml(args.ambiguity)
    today = dt.date.fromisoformat(args.today)
    golden = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]

    # 判定器：借用 evaluate 的等价判定（mock 引擎回放真实输出）
    verdicts: Dict[str, List[bool]] = defaultdict(list)
    why_stat: Counter = Counter()
    detail: Dict[str, Dict[str, Any]] = {}

    for i in range(args.repeat):
        print(f"[RUN {i + 1}/{args.repeat}] {len(golden)} 条 ...")
        outs = run_once(golden, registry, ambiguity, llm, today)

        class Replay:
            """把真实模型输出回放给评估器，复用其等价判定逻辑。"""

            name = "replay_real_llm"

            # ★ 反问/拒绝/问口径是管线后处理产生的状态，模型原始 DSL 里没有，
            #   回放时必须按状态回填，否则「正确反问」会被判成失败。
            _MAP = {"need_clarify": {"intent": "need_clarify"},
                    "out_of_scope": {"intent": "out_of_scope"},
                    "meta": {"intent": "meta_question"},
                    "denied": {"intent": "out_of_scope"}}

            def query(self, case, ctx):
                o = outs[case["id"]]
                dsl = self._MAP.get(o["status"]) or o["raw"] or {"intent": "error"}
                return {"dsl": dsl, "status": o["status"], "latency_ms": o["ms"]}

        rep = evaluate(golden, Replay(), registry, None, today)
        for case, row in zip(golden, rep["rows"]):
            cid = case["id"]
            ok = bool(row["correct"])
            verdicts[cid].append(ok)
            if not ok:
                for w in diff_fields(case, outs[cid]["raw"], outs[cid]["status"],
                                     outs[cid]["caliber"]):
                    # 去掉括号里的具体取值，只统计「错在哪一类」
                    why_stat[w.split("（")[0]] += 1
            detail.setdefault(cid, {"question": case.get("question"),
                                    "gold_intent": case.get("intent"),
                                    "fail": 0, "why": Counter(),
                                    "last_raw": outs[cid]["raw"],
                                    "last_status": outs[cid]["status"]})
            if not ok:
                detail[cid]["fail"] += 1
                for w in diff_fields(case, outs[cid]["raw"], outs[cid]["status"],
                                     outs[cid]["caliber"]):
                    detail[cid]["why"][w] += 1

    n = len(golden)
    stable = [c for c in golden if verdicts[c["id"]] and not any(verdicts[c["id"]])]
    flaky = [c for c in golden if verdicts[c["id"]] and any(verdicts[c["id"]])
             and not all(verdicts[c["id"]])]
    per_run = [sum(1 for c in golden if verdicts[c["id"]][i]) / n
               for i in range(args.repeat)]

    print("\n" + "=" * 76)
    print(f"正确率：每次 {[round(x * 100, 1) for x in per_run]}%　"
          f"均值 {round(sum(per_run) / len(per_run) * 100, 1)}%　"
          f"波动 ±{round((max(per_run) - min(per_run)) * 100 / 2, 1)}pt")
    print(f"稳定失败 {len(stable)} 条 / 抖动失败 {len(flaky)} 条 / 全对 {n - len(stable) - len(flaky)} 条")
    print("\n失败原因分布（按出现次数）：")
    for w, c in why_stat.most_common():
        print(f"  {c:>3}  {w}")
    print("\n稳定失败清单：")
    for c in stable:
        d = detail[c["id"]]
        print(f"  {c['id']} [{d['gold_intent']}] {d['question']}")
        print(f"        {dict(d['why'])}")

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        json.dump({"per_run": per_run,
                   "stable": [c["id"] for c in stable],
                   "flaky": [c["id"] for c in flaky],
                   "why_stat": dict(why_stat),
                   "detail": {k: {"question": v["question"], "fail": v["fail"],
                                  "why": dict(v["why"])}
                              for k, v in detail.items()}},
                  open(args.output, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    if args.md:
        os.makedirs(os.path.dirname(args.md) or ".", exist_ok=True)
        L = ["# 失败诊断报告", "",
             f"- 模型：{model}　重复 {args.repeat} 次　用例 {n} 条　基线日期 {args.today}",
             f"- 每次正确率：{[round(x * 100, 1) for x in per_run]}%　"
             f"均值 {round(sum(per_run) / len(per_run) * 100, 1)}%", "",
             "## 1. 失败原因分布", "", "| 原因 | 次数 |", "|---|---|"]
        L += [f"| {w} | {c} |" for w, c in why_stat.most_common()]
        L += ["", f"## 2. 稳定失败（{len(stable)} 条，系统性缺陷）", "",
              "| 用例 | 期望意图 | 问题 | 失败原因 |", "|---|---|---|---|"]
        for c in stable:
            d = detail[c["id"]]
            L.append(f"| {c['id']} | {d['gold_intent']} | {d['question']} | "
                     f"{'；'.join(d['why'])} |")
        L += ["", f"## 3. 抖动失败（{len(flaky)} 条，模型不确定性）", "",
              "| 用例 | 失败/总次数 | 问题 |", "|---|---|---|"]
        for c in flaky:
            d = detail[c["id"]]
            L.append(f"| {c['id']} | {d['fail']}/{args.repeat} | {d['question']} |")
        L += ["", "> 稳定失败靠改代码/提示词/注册表解决；",
              "> 抖动失败靠自洽投票（同一题跑多次取多数）或更强的约束解决。", ""]
        open(args.md, "w", encoding="utf-8").write("\n".join(L))
        print(f"[INFO] 报告: {args.md}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
