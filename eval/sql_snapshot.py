#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SQL 逐字回归（golden 集 → SQL 快照比对）。

用途：
    改动 SQL 生成器（加 JOIN、改列限定、改方言）时，**先证明没把已经对的 SQL 改坏**。
    结果正确率是端到端指标，波动大、定位慢；SQL 快照是逐字的，改一下就立刻红。

判定口径：
    只对「应当出数」的用例生成 SQL（歧义/越权/问口径/超范围不产生 SQL）。
    快照与当前生成的 SQL 必须**逐字相同**，任何差异都要人工确认后再 --write。

用法:
    python sql_snapshot.py --write     # 首次生成/人工确认后更新基线
    python sql_snapshot.py            # 校验（CI 用，有差异则非零退出）
    python sql_snapshot.py --diff G0004
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "engine"))

from dsl import dsl_from_dict  # noqa: E402
from sql_builder import SQLBuilder  # noqa: E402

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DEFAULT_REGISTRY = os.path.join(_HERE, "..", "semantic-layer", "metric_registry.yaml")
DEFAULT_GOLDEN = os.path.join(_HERE, "golden_set_sample.jsonl")
DEFAULT_SNAPSHOT = os.path.join(_HERE, "sql_snapshot.json")
DEFAULT_TODAY = "2026-10-14"


def _range_of(case: Dict[str, Any]) -> Dict[str, Any]:
    tr = case.get("time_range")
    if isinstance(tr, list) and len(tr) == 2:
        return {"type": "absolute", "start": tr[0], "end": tr[1]}
    if isinstance(tr, str):
        return {"type": tr}
    return {"type": "last_month"}


def build_sqls(golden: List[Dict[str, Any]], registry: Dict[str, Any],
               today: str = DEFAULT_TODAY) -> Dict[str, Dict[str, Any]]:
    builder = SQLBuilder(registry)
    out: Dict[str, Dict[str, Any]] = {}
    for c in golden:
        if c.get("requires_clarify") or c.get("permission_expect") == "deny":
            continue
        if c.get("intent") not in ("query_metric", "compare", "rank", "trend",
                                   "attribution"):
            continue
        dsl_dict = {
            "intent": c.get("intent"),
            "metrics": c.get("metrics") or [],
            "dimensions": c.get("dimensions") or [],
            "filters": c.get("filters") or [],
            "time": {
                "granularity": c.get("time_granularity") or "month",
                "range": _range_of(c),
                "date_basis": c.get("date_basis"),
                "compare": c.get("compare") or "none",
            },
            "options": {
                "dept_basis": c.get("dept_basis"),
                "dedup": c.get("dedup"),
                "top_n": c.get("top_n"),
                "order_by": c.get("order_by"),
            },
            "confidence": 0.95,
        }
        try:
            dsl = dsl_from_dict(dsl_dict, registry)
            built = builder.build(dsl, ctx={"role": c.get("role"),
                                            "dept_scope": c.get("dept_scope") or ["*"]})
            sql = built["sql"]
        except Exception as e:  # noqa: BLE001
            sql = f"__ERROR__: {type(e).__name__}: {e}"
        out[c["id"]] = {"question": c.get("question"), "sql": sql,
                        "sha1": hashlib.sha1(sql.encode("utf-8")).hexdigest()[:12]}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SQL 逐字回归")
    ap.add_argument("--golden", default=DEFAULT_GOLDEN)
    ap.add_argument("--registry", default=DEFAULT_REGISTRY)
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    ap.add_argument("--today", default=DEFAULT_TODAY)
    ap.add_argument("--write", action="store_true", help="更新基线快照")
    ap.add_argument("--diff", help="打印指定用例的完整 SQL")
    args = ap.parse_args()

    if yaml is None:
        raise SystemExit("需要 PyYAML: pip install pyyaml")
    registry = yaml.safe_load(open(args.registry, encoding="utf-8"))
    golden = [json.loads(l) for l in open(args.golden, encoding="utf-8") if l.strip()]

    cur = build_sqls(golden, registry, args.today)

    if args.diff:
        if args.diff not in cur:
            print(f"用例 {args.diff} 不产生 SQL（歧义/越权/问口径）")
            return 0
        print(cur[args.diff]["sql"])
        return 0

    if args.write:
        with open(args.snapshot, "w", encoding="utf-8") as f:
            json.dump({"today": args.today, "cases": cur}, f,
                      ensure_ascii=False, indent=2)
        print(f"[INFO] 已写入基线 {args.snapshot}（{len(cur)} 条）")
        return 0

    if not os.path.exists(args.snapshot):
        print(f"[FAIL] 缺少基线 {args.snapshot}，先跑 --write 并人工确认")
        return 1

    base = json.load(open(args.snapshot, encoding="utf-8"))["cases"]
    diffs = []
    for cid, item in cur.items():
        old = (base.get(cid) or {}).get("sql")
        if old != item["sql"]:
            diffs.append((cid, old, item["sql"]))

    if not diffs:
        print(f"[OK] SQL 逐字一致（{len(cur)} 条）")
        return 0

    print(f"[FAIL] {len(diffs)} 条 SQL 发生变化：")
    for cid, old, new in diffs:
        print(f"\n--- {cid} ---")
        print("  旧: " + str(old))
        print("  新: " + str(new))
    print("\n若变更是预期的，人工核对后跑 --write 更新基线。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
