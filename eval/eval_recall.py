#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指标召回离线评估（recall@k / MRR）。

为什么必须单独评估召回：
    端到端「结果正确率」把两类失败混在一起 ——
      ① 候选里根本没有正确指标（召回失败，模型再强也选不对）
      ② 候选里有但模型选错/口径写错（生成失败）
    不拆开就无法回答「该改召回还是该换模型」。

判定口径：
    full_hit  —— golden 的 metrics 全部落在候选集内（多指标题要求全覆盖）
    any_hit   —— 至少命中一个（宽松口径，用于看召回"半对"的程度）
    MRR       —— 第一个正确指标排名的倒数均值

用法:
    python eval_recall.py --golden golden_set_sample.jsonl \
        --registry ../semantic-layer/metric_registry.yaml --top-k 8
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "engine"))

from pipeline import recall_metrics  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def _load_jsonl(path: str) -> List[Dict[str, Any]]:
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def evaluate_recall(golden: List[Dict[str, Any]], registry: Dict[str, Any],
                    ks=(1, 3, 5, 8), top_k: int = 8) -> Dict[str, Any]:
    """只评估「应当给出指标」的用例。

    反问题（requires_clarify）仍保留：它的 golden metrics 记的是澄清后的目标指标，
    召回若拿不到它，澄清完也选不对。

    ★ 排除 intent=out_of_scope 的用例：这类题**本来就该答「暂不支持」**，
      它的 metrics 只是"用户想问的那个指标"，不是"系统应当召回的指标"。
      把它算进分母，会让召回率永远卡在 96.7%，而且掩盖真正的召回缺陷
      （实测：G0016「床位使用率」被误判成召回失败，其实是语义层没建床日事实表）。
      这类用例单独统计，见 excluded_unsupported。
    """
    cases = [c for c in golden
             if c.get("metrics") and c.get("intent") != "out_of_scope"]
    excluded = [c for c in golden
                if c.get("metrics") and c.get("intent") == "out_of_scope"]
    # 评测集与语义层的漂移检查：golden 引用了未注册的指标，召回再准也命不中，
    # 这类用例必须单列，否则会被错误归因成「召回算法不行」。
    metric_codes = {m.get("code") for m in (registry.get("metrics") or [])}
    unknown_gold = [
        {"id": c.get("id"), "question": c.get("question"), "metric": m}
        for c in cases for m in (c.get("metrics") or [])
        if m and m not in metric_codes
    ]
    unknown_ids = {u["id"] for u in unknown_gold}
    per_k = {k: 0 for k in ks}
    per_k_any = {k: 0 for k in ks}
    rr_sum = 0.0
    rows: List[Dict[str, Any]] = []

    for c in cases:
        gold = [m for m in (c.get("metrics") or []) if m]
        cands = recall_metrics(c.get("question", ""), registry, top_k=top_k)
        codes = [x["code"] for x in cands]

        ranks = [codes.index(g) + 1 for g in gold if g in codes]
        rr = 1.0 / min(ranks) if ranks else 0.0
        rr_sum += rr

        full = all(g in codes for g in gold)
        any_hit = bool(ranks)
        for k in ks:
            head = codes[:k]
            if all(g in head for g in gold):
                per_k[k] += 1
            if any(g in head for g in gold):
                per_k_any[k] += 1

        rows.append({
            "id": c.get("id"), "question": c.get("question"),
            "gold": gold, "candidates": codes,
            "full_hit": full, "any_hit": any_hit, "mrr": round(rr, 4),
            "gold_rank": min(ranks) if ranks else None,
        })

    n = len(cases)
    return {
        "total": n,
        "top_k": top_k,
        "recall_full": {k: round(per_k[k] / n, 4) if n else 0.0 for k in ks},
        "recall_any": {k: round(per_k_any[k] / n, 4) if n else 0.0 for k in ks},
        "mrr": round(rr_sum / n, 4) if n else 0.0,
        "unknown_gold": unknown_gold,
        "unknown_case_ids": sorted(unknown_ids),
        # 语义层明确不支持的用例（不进召回分母，单独列出）
        "excluded_unsupported": [
            {"id": c.get("id"), "question": c.get("question"),
             "metrics": c.get("metrics")}
            for c in excluded],
        "misses": [r for r in rows if not r["full_hit"]],
        "rows": rows,
    }


def main():
    ap = argparse.ArgumentParser(description="指标召回离线评估")
    ap.add_argument("--golden", required=True)
    ap.add_argument("--registry", required=True)
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--output", help="结果 JSON 输出路径")
    ap.add_argument("--show-miss", type=int, default=20)
    args = ap.parse_args()

    if yaml is None:
        raise SystemExit("需要 PyYAML: pip install pyyaml")
    registry = yaml.safe_load(open(args.registry, encoding="utf-8"))
    golden = _load_jsonl(args.golden)

    rep = evaluate_recall(golden, registry, top_k=args.top_k)

    print("=" * 68)
    print(f"样本数(应召回) : {rep['total']}")
    if rep["excluded_unsupported"]:
        print(f"已排除(明确不支持) : {len(rep['excluded_unsupported'])} 条"
              f"（intent=out_of_scope，不该出数）")
    print(f"候选上限 top_k : {rep['top_k']}")
    print("-" * 68)
    print("recall@k (全部命中 / 至少一个):")
    for k in sorted(rep["recall_full"]):
        print(f"  @{k:<2}  {rep['recall_full'][k]:>7.1%}   "
              f"{rep['recall_any'][k]:>7.1%}")
    print(f"MRR            : {rep['mrr']:.4f}")
    print("-" * 68)

    if rep["unknown_gold"]:
        print(f"[本体缺口] {len(rep['unknown_case_ids'])} 条用例引用了语义层未注册的指标：")
        for u in rep["unknown_gold"]:
            print(f"  {u['id']}  {u['metric']}   问：{u['question']}")
        print("  → 这类用例的失败不是召回算法问题，需先在 metric_registry.yaml 注册指标")
        print("-" * 68)

    miss = rep["misses"]
    if miss:
        print(f"召回不完整 {len(miss)} 条（最多显示 {args.show_miss} 条）:")
        for r in miss[:args.show_miss]:
            tag = "  [本体缺口]" if r["id"] in rep["unknown_case_ids"] else ""
            print(f"  {r['id']}  期望={r['gold']}{tag}")
            print(f"        问：{r['question']}")
            print(f"        候选：{r['candidates']}")
    else:
        print("召回完整：所有用例的目标指标都进了候选集")

    if rep["excluded_unsupported"]:
        print("-" * 68)
        print("语义层明确不支持（不进召回分母，期望答「暂不支持 + 指标申请」）：")
        for u in rep["excluded_unsupported"]:
            print(f"  {u['id']}  {u['metrics']}  问：{u['question']}")

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 报告已写入 {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
