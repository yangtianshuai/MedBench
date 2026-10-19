#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Golden 集覆盖度体检 + 采集缺口清单（docs/09 §2/§3 + §12 checklist 的脚本化）。

★ 定位：本脚本**不生成题目**。
  标注规范明确限制「标注者构造 ≤15%」、P0 来源必须是院周会/运营会/质控会纪要与月报季报，
  用脚本批量造 300 条会直接违反红线并让评估失真。
  正确的做法是：脚本负责**体检与算缺口**，人负责按缺口清单去院内采集真实问法。

用法：
    python eval/check_coverage.py \
        --input eval/golden_set_sample.jsonl \
        --registry semantic-layer/metric_registry.yaml \
        --target 300 \
        --md eval/reports/golden_gap.md

退出码：0 = 达标；1 = 有缺口（可接 CI 卡住入库）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")
sys.path.insert(0, os.path.join(_ROOT, "engine"))

try:
    import yaml
except ImportError:
    yaml = None

# docs/09 §3.2 场景占比要求（下限）
SCENE_MIN = {"歧义": 0.20, "越权": 0.05, "超范围": 0.05, "问口径": 0.05,
             "时间变体": 0.15, "排名对比": 0.10}
# docs/09 §2 来源占比要求
SOURCE_TARGET = {"meeting": 0.35, "report": 0.20, "interview": 0.15,
                 "ticket": 0.15, "synthetic": 0.15}
SOURCE_MAX = {"synthetic": 0.15}
# docs/09 §3.1 指标覆盖
PER_METRIC_MIN = 8
PER_RATIO_MIN = 12
COMBO_MIN = 20
# docs/09 §3.3 难度
DIFF_TARGET = {"easy": 0.4, "medium": 0.4, "hard": 0.2}
# 时间表达：10 类每类 ≥3，其中这三类各 ≥5
TIME_MIN = 3
TIME_MIN_HOT = {"year_to_date": 5, "first_three_quarters": 5,
                "same_period_last_year": 5}
TIME_CLASSES = ["last_month", "current_month", "last_quarter", "last_n_months",
                "year_to_date", "first_three_quarters", "same_period_last_year",
                "last_3_years", "week_before_last", "absolute"]


def time_key(case: Dict[str, Any]) -> str:
    """time_range 可能是字符串 / {"type": ...} / 列表，统一取类型名。"""
    t = case.get("time_range")
    if isinstance(t, dict):
        t = t.get("type")
    if isinstance(t, list):
        t = t[0] if t else None
        if isinstance(t, dict):
            t = t.get("type")
    return str(t) if t else ""


def scene_of(case: Dict[str, Any]) -> List[str]:
    """一条用例可能同时属于多个场景（占比按「命中」计）。"""
    s: List[str] = []
    if case.get("requires_clarify"):
        s.append("歧义")
    if (case.get("permission_expect") or "allow") != "allow":
        s.append("越权")
    if case.get("intent") == "out_of_scope":
        s.append("超范围")
    if case.get("intent") == "meta_question":
        s.append("问口径")
    if time_key(case) in TIME_CLASSES or case.get("time_expr"):
        if time_key(case) in ("year_to_date", "first_three_quarters",
                                      "same_period_last_year", "last_quarter",
                                      "last_3_years", "week_before_last"):
            s.append("时间变体")
    if case.get("intent") in ("rank", "compare"):
        s.append("排名对比")
    if not s:
        s.append("常规")
    elif not (set(s) & {"歧义", "越权", "超范围", "问口径", "时间变体", "排名对比"}):
        s.append("常规")
    return s


def check(golden: List[Dict[str, Any]], registry: Dict[str, Any],
          target: int) -> Dict[str, Any]:
    n = len(golden)
    metrics = {m["code"]: m for m in (registry.get("metrics") or [])}
    ratio_codes = {c for c, m in metrics.items() if m.get("type") == "ratio"}

    per_metric = Counter()
    for c in golden:
        for code in (c.get("metrics") or []):
            per_metric[code] += 1
    combo = sum(1 for c in golden if len(c.get("metrics") or []) >= 2)

    scene = Counter()
    for c in golden:
        for s in scene_of(c):
            scene[s] += 1

    time_cls = Counter(time_key(c) for c in golden)
    diff = Counter(c.get("difficulty") for c in golden)
    source = Counter(c.get("source") or "unknown" for c in golden)

    gaps: List[Dict[str, Any]] = []

    def add(kind: str, item: str, need: int, have: int) -> None:
        if need > have:
            gaps.append({"kind": kind, "item": item, "have": have,
                         "need": need, "missing": need - have})

    add("规模", "总条数", target, n)
    for code in metrics:
        req = PER_RATIO_MIN if code in ratio_codes else PER_METRIC_MIN
        add("指标", f"{code}（{'ratio' if code in ratio_codes else 'atomic'}）",
            req, per_metric.get(code, 0))
    add("指标组合", "多指标查询条数", COMBO_MIN, combo)
    for s, ratio in SCENE_MIN.items():
        add("场景", s, int(round(ratio * target)), scene.get(s, 0))
    for t in TIME_CLASSES:
        req = TIME_MIN_HOT.get(t, TIME_MIN)
        add("时间表达", t, req, time_cls.get(t, 0))
    for d, ratio in DIFF_TARGET.items():
        add("难度", d, int(round(ratio * target)), diff.get(d, 0))
    for src, ratio in SOURCE_TARGET.items():
        have = source.get(src, 0)
        need = int(round(ratio * target))
        if src in SOURCE_MAX:
            # 上限约束：构造题不得超过 15%
            over = have - int(round(SOURCE_MAX[src] * target))
            if over > 0:
                gaps.append({"kind": "来源占比超限", "item": src, "have": have,
                             "need": f"≤{int(round(SOURCE_MAX[src] * target))}",
                             "missing": over})
        else:
            add("来源", src, need, have)

    return {
        "n": n, "target": target,
        "per_metric": dict(per_metric), "combo": combo,
        "scene": dict(scene), "scene_pct": {k: round(100 * v / n, 1) for k, v in scene.items()},
        "time": dict(time_cls),
        "difficulty": dict(diff),
        "difficulty_pct": {k: round(100 * v / n, 1) for k, v in diff.items()},
        "source": dict(source),
        "source_pct": {k: round(100 * v / n, 1) for k, v in source.items()},
        "gaps": gaps,
        "ok": not gaps,
    }


def write_md(path: str, r: Dict[str, Any]) -> None:
    L = ["# Golden 集覆盖度体检与采集缺口", "",
         f"- 现有 {r['n']} 条 / 目标 {r['target']} 条　"
         f"状态：**{'达标' if r['ok'] else '存在缺口'}**", "",
         "## 1. 现状", "",
         "| 维度 | 现状 |", "|---|---|",
         f"| 场景分布 | {r['scene_pct']} |",
         f"| 难度分布 | {r['difficulty_pct']} |",
         f"| 来源分布 | {r['source_pct']} |",
         f"| 时间表达 | {r['time']} |",
         f"| 多指标组合 | {r['combo']} 条（要求 ≥{COMBO_MIN}） |", "",
         "## 2. 缺口清单（按此去院内采集）", "",
         "| 类别 | 项目 | 现有 | 需要 | 还缺 |", "|---|---|---|---|---|"]
    for g in r["gaps"]:
        L.append(f"| {g['kind']} | {g['item']} | {g['have']} | {g['need']} | "
                 f"**{g['missing']}** |")
    if not r["gaps"]:
        L.append("| — | 全部达标 | | | |")
    L += ["", "## 3. 采集指引", "",
          "- **P0（55%）**：院周会/运营会/质控会纪要 35% + 月报季报图表反推 20%",
          "- **P1（30%）**：科室主任访谈 ≥5 人各 20 条 15% + 历史取数工单改写 15%",
          "- **P2（≤15%）**：标注者构造，必须是业务口吻，禁止技术口吻", "",
          "⚠️ 缺口**不能靠脚本编造**：构造占比超过 15% 会让分布偏斜、评估失真"
          "（docs/09 §2.1）。本脚本只负责算缺口。", ""]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def main() -> int:
    ap = argparse.ArgumentParser(description="Golden 集覆盖度体检")
    ap.add_argument("--input", default=os.path.join(_HERE, "golden_set_sample.jsonl"))
    ap.add_argument("--registry", default=os.path.join(
        _ROOT, "semantic-layer", "metric_registry.yaml"))
    ap.add_argument("--target", type=int, default=300)
    ap.add_argument("--md", help="输出缺口清单 Markdown")
    ap.add_argument("--output", help="输出 JSON")
    args = ap.parse_args()

    if yaml is None:
        print("[FATAL] 需要 pyyaml")
        return 2
    registry = yaml.safe_load(open(args.registry, encoding="utf-8"))
    golden = [json.loads(l) for l in open(args.input, encoding="utf-8") if l.strip()]
    r = check(golden, registry, args.target)

    print(f"现有 {r['n']} 条 / 目标 {r['target']} 条")
    print(f"场景：{r['scene_pct']}")
    print(f"难度：{r['difficulty_pct']}")
    print(f"来源：{r['source_pct']}")
    print(f"缺口 {len(r['gaps'])} 项：")
    for g in r["gaps"][:20]:
        print(f"  - [{g['kind']}] {g['item']}：现有 {g['have']}，需 {g['need']}，"
              f"缺 {g['missing']}")
    if len(r["gaps"]) > 20:
        print(f"  ... 另 {len(r['gaps']) - 20} 项见报告")

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        json.dump(r, open(args.output, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"[INFO] JSON: {args.output}")
    if args.md:
        os.makedirs(os.path.dirname(args.md) or ".", exist_ok=True)
        write_md(args.md, r)
        print(f"[INFO] 缺口清单: {args.md}")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
