#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Golden 集评估执行器（POC-B 核心工具）

设计要点（严谨性优先）：
  1. 评估器自身必须可信 —— 内置 MockOracle（应得 100%）与 MockPerturbed（注入已知错误）
     两种引擎，用于验证评估逻辑本身没写错。跑评估前必须先跑这两个自测。
  2. 结果正确率以【执行结果比对】为准（需 --db）；无库时退化为 DSL 等价比对，
     并在报告中明确标注判定方式，不允许把 DSL 等价冒充为结果正确。
  3. 越权/超范围用例单独统计，拒绝正确率必须 100%，任一条漏放即整体判失败。

用法:
    # 自测评估器（必做）
    python run_golden.py --golden golden_set_sample.jsonl --engine mock_oracle
    python run_golden.py --golden golden_set_sample.jsonl --engine mock_perturbed

    # 真实评估（问数引擎需提供 HTTP 接口）
    python run_golden.py --golden golden_set_v1.jsonl \
        --engine http --endpoint http://10.0.0.1:8000/v1/query \
        --registry ../semantic-layer/metric_registry.yaml \
        --db "mysql+pymysql://user:pwd@host:9030/dwd" \
        --output reports/pocb_g3_qwen14b.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from collections import Counter
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine"))

from dsl import QueryDSL, dsl_from_dict, validate_dsl  # noqa: E402
from sql_builder import SQLBuilder  # noqa: E402

try:
    import yaml
except ImportError:
    yaml = None


# ────────────────────────────────────────────────────────────
# 引擎适配
# ────────────────────────────────────────────────────────────
class BaseEngine:
    name = "base"

    def query(self, case: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """返回 {"dsl": {...}, "latency_ms": int, "raw": ...}"""
        raise NotImplementedError


class MockOracleEngine(BaseEngine):
    """完美引擎：直接返回标准答案。用于验证评估器应得 100%。"""

    name = "mock_oracle"

    def query(self, case, ctx):
        dsl = {
            "intent": case.get("intent"),
            "metrics": case.get("metrics") or [],
            "dimensions": case.get("dimensions") or [],
            "filters": case.get("filters") or [],
            "time": {
                "granularity": case.get("time_granularity") or "month",
                "range": _range_of(case),
                "date_basis": case.get("date_basis"),
                "compare": case.get("compare") or "none",
            },
            "options": {
                "dept_basis": case.get("dept_basis"),
                "dedup": case.get("dedup"),
                "top_n": case.get("top_n"),
                "order_by": case.get("order_by"),
            },
            "confidence": 0.95,
        }
        # 越权用例：理想引擎必须拒绝
        if case.get("permission_expect") == "deny":
            return {"dsl": {"intent": "out_of_scope", "ask": None}, "latency_ms": 120}
        if case.get("intent") == "out_of_scope":
            dsl = {"intent": "out_of_scope"}
        if case.get("requires_clarify"):
            dsl = {"intent": "need_clarify", "ask": "...",
                   "clarify_options": [case.get("clarify_resolution")]}
        return {"dsl": dsl, "latency_ms": 120}


class MockPerturbedEngine(BaseEngine):
    """注入已知错误率的引擎。用于验证评估器能准确测出退化。

    注入项（与 --rate 对应）：
      - 10% 指标选错
      - 50% 该反问时不反问（直接给答案）
      - 20% 越权用例未拒绝
      - 5%  口径参数写错
    """

    name = "mock_perturbed"

    def query(self, case, ctx):
        oracle = MockOracleEngine().query(case, ctx)
        dsl = oracle["dsl"]
        # 确定性扰动：按用例序号取模，保证可复现、可预期
        n = int(str(case.get("id", "0")).lstrip("G") or 0)

        # 50%：该反问却不反问（"错得很自信"）
        if dsl.get("intent") == "need_clarify" and n % 2 == 0:
            dsl = {"intent": "query_metric", "metrics": case.get("metrics") or [],
                   "dimensions": case.get("dimensions") or [],
                   "time": {"granularity": case.get("time_granularity") or "month",
                            "range": _range_of(case),
                            "date_basis": case.get("date_basis")},
                   "options": {"dept_basis": case.get("dept_basis"),
                               "dedup": case.get("dedup")},
                   "confidence": 0.95}

        # 约 33%：越权用例未拒绝
        if case.get("permission_expect") == "deny" and n % 3 == 0:
            dsl = {"intent": "query_metric", "metrics": case.get("metrics") or [],
                   "time": {"range": _range_of(case)}, "options": {}}

        # 10%：指标选错
        if dsl.get("metrics") and n % 10 == 7:
            dsl["metrics"] = ["ip_admit_cnt"]

        # 5%：口径写错
        if n % 20 == 13 and dsl.get("time"):
            dsl["time"]["date_basis"] = "settle"

        return {"dsl": dsl, "latency_ms": 180}


class HttpEngine(BaseEngine):
    """真实问数引擎：POST {question, role, dept_scope} → {dsl}"""

    name = "http"

    def __init__(self, endpoint: str, timeout: int = 30):
        self.endpoint = endpoint
        self.timeout = timeout

    def query(self, case, ctx):
        import urllib.request
        payload = json.dumps({
            "question": case.get("question"),
            "role": ctx.get("role"),
            "dept_scope": ctx.get("dept_scope"),
            "today": ctx.get("today"),
        }).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint, data=payload,
            headers={"Content-Type": "application/json"})
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return {"dsl": body.get("dsl") or body, "latency_ms": int((time.time() - t0) * 1000)}


def _date_of(today: Any):
    """today 可能是 str / date，统一成 date（保证 build 结果可复现）。"""
    if isinstance(today, dt.date):
        return today
    try:
        return dt.date.fromisoformat(str(today))
    except ValueError:
        return dt.date.today()


def _range_of(case: Dict[str, Any]) -> Dict[str, Any]:
    tr = case.get("time_range")
    if isinstance(tr, list) and len(tr) == 2:
        return {"type": "absolute", "start": tr[0], "end": tr[1]}
    if isinstance(tr, str):
        return {"type": tr}
    return {"type": "last_month"}


# ────────────────────────────────────────────────────────────
# 判定
# ────────────────────────────────────────────────────────────
def dsl_equivalent(pred: Dict[str, Any], gold: Dict[str, Any]) -> bool:
    """DSL 等价判定（无数据库时的降级方案）。"""
    pi, gi = pred.get("intent"), gold.get("intent")
    if pi != gi:
        return False
    if pi in ("need_clarify", "out_of_scope", "meta_question", "unsupported_metric"):
        return True

    if set(pred.get("metrics") or []) != set(gold.get("metrics") or []):
        return False
    if set(pred.get("dimensions") or []) != set(gold.get("dimensions") or []):
        return False

    pt = pred.get("time") or {}
    gt = {"granularity": gold.get("time_granularity") or "month",
          "date_basis": gold.get("date_basis"), "compare": gold.get("compare") or "none"}
    if pt.get("date_basis") != gt["date_basis"]:
        return False
    if (pt.get("compare") or "none") != gt["compare"]:
        return False

    po = pred.get("options") or {}
    if (po.get("dept_basis") or None) != (gold.get("dept_basis") or None):
        return False
    if (po.get("dedup") or None) != (gold.get("dedup") or None):
        return False

    # 过滤条件（顺序无关）
    def norm(fs):
        return sorted(json.dumps(f, sort_keys=True, ensure_ascii=False)
                      for f in (fs or []))
    if norm(pred.get("filters")) != norm(gold.get("filters")):
        return False
    return True


def result_match(cur: List, exp: List, tolerance: float = 0.01) -> bool:
    """执行结果比对（有数据库时的权威判定）。"""
    if len(cur) != len(exp):
        return False
    for a, b in zip(cur, exp):
        ra, rb = list(a), list(b)
        if len(ra) != len(rb):
            return False
        for x, y in zip(ra, rb):
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                if abs(float(x) - float(y)) > max(tolerance, abs(float(y)) * 1e-6):
                    return False
            elif str(x) != str(y):
                return False
    return True


# ────────────────────────────────────────────────────────────
# 主评估
# ────────────────────────────────────────────────────────────
def evaluate(golden: List[Dict[str, Any]], engine: BaseEngine,
             registry: Optional[Dict[str, Any]] = None,
             db_url: Optional[str] = None,
             today: str = "2026-10-14") -> Dict[str, Any]:
    builder = SQLBuilder(registry) if registry else None
    conn = None
    if db_url:
        try:
            from sqlalchemy import create_engine, text  # type: ignore
            conn = create_engine(db_url).connect()
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] 无法连接数据库，降级为 DSL 等价比对: {e}")
            conn = None

    judge = "result" if conn else "dsl_equivalent"
    rows: List[Dict[str, Any]] = []
    latencies: List[int] = []

    for case in golden:
        ctx = {"role": case.get("role"), "dept_scope": case.get("dept_scope"),
               "today": today}
        resp = engine.query(case, ctx)
        pred = resp.get("dsl") or {}
        latencies.append(int(resp.get("latency_ms", 0)))

        gold_intent = case.get("intent")
        need = bool(case.get("requires_clarify"))

        # 意图可接受集合：
        #   - 歧义题的正确行为是先反问，而非直接作答
        #   - 越权题的正确行为是拒绝，此时 gold.intent 记的是「用户意图」
        acceptable = {gold_intent}
        if need:
            acceptable.add("need_clarify")
        if case.get("permission_expect") == "deny":
            acceptable.add("out_of_scope")

        status = resp.get("status")
        rec = {
            "id": case.get("id"),
            "question": case.get("question"),
            "status": status,
            "gold_intent": gold_intent,
            "pred_intent": pred.get("intent"),
            "intent_ok": pred.get("intent") in acceptable,
        }

        # 可执行性：DSL 能否通过语义层校验并生成 SQL
        # ★ 同时用 build() 回显的 caliber 补全口径：
        #   golden 记的是「语义层最终采用的口径」，而提示词允许模型省略口径
        #   （约定：省略即取指标默认）。若直接拿模型原始输出比对，
        #   「按约定省略」会被误判成「口径写错」，真实模型会被系统性低估。
        exec_ok, sql = False, None
        resolved_pred = dict(pred)
        if builder and pred.get("intent") in ("query_metric", "compare", "rank",
                                              "trend", "attribution"):
            try:
                dsl_obj = dsl_from_dict(pred, registry)
                exec_ok = not validate_dsl(dsl_obj, registry)
                if exec_ok:
                    built = builder.build(dsl_obj, ctx=ctx,
                                          today=_date_of(today))
                    sql = built.get("sql")
                    cal = built.get("caliber") or {}
                    t = dict(resolved_pred.get("time") or {})
                    o = dict(resolved_pred.get("options") or {})
                    if cal.get("date_basis") is not None:
                        t["date_basis"] = cal["date_basis"]
                    if cal.get("dept_basis") is not None:
                        o["dept_basis"] = cal["dept_basis"]
                    if cal.get("dedup") is not None:
                        o["dedup"] = cal["dedup"]
                    resolved_pred["time"], resolved_pred["options"] = t, o
            except Exception:  # noqa: BLE001
                exec_ok = False
        rec["dsl_executable"] = exec_ok

        # ── 正确性判定：按用例类型分流 ──────────────────
        asked = pred.get("intent") == "need_clarify"
        # ★ 拒绝必须看引擎状态：越权/超范围被正确拦截时，DSL 可能是
        #   {"intent":"out_of_scope"}（早退留痕）或根本没有 DSL，
        #   只看 DSL intent 会把「正确拒绝」判成失败。
        denied = status in ("denied", "out_of_scope") or pred.get("intent") in (
            "out_of_scope", "need_clarify", "unsupported_metric")

        if need:
            # 歧义题：正确行为是反问
            correct = asked
        elif case.get("permission_expect") == "deny":
            # 越权题：正确行为是拒绝
            correct = denied
        elif gold_intent in ("meta_question", "out_of_scope"):
            # 问口径 / 超范围：正确行为是识别意图并给出对应响应
            correct = (pred.get("intent") == gold_intent)
        elif conn and sql and case.get("expected_sql"):
            # 有库时以执行结果为准（权威判定）
            try:
                cur = [tuple(r) for r in conn.execute(
                    __import__("sqlalchemy").text(sql)).fetchall()]
                exp = [tuple(r) for r in conn.execute(
                    __import__("sqlalchemy").text(case["expected_sql"])).fetchall()]
                correct = result_match(cur, exp)
            except Exception as e:  # noqa: BLE001
                correct = False
                rec["error"] = str(e)[:120]
        else:
            # 无库时降级为 DSL 等价（报告中会明确标注非结果比对）
            correct = dsl_equivalent(resolved_pred, case) and exec_ok

        rec["correct"] = bool(correct)

        # 反问行为
        rec["need_clarify"] = need
        rec["asked"] = asked
        rec["clarify_ok"] = (asked == need)
        rec["false_ask"] = (asked and not need)

        # 越权/超范围拒绝
        restricted = case.get("permission_expect") == "deny" or \
            case.get("intent") == "out_of_scope"
        rec["restricted"] = restricted
        if restricted:
            # ★ 必须看引擎状态：越权被正确拒绝时 status=denied，
            #   而引擎为保住意图统计把 denied 映射成 query_metric，
            #   只看 DSL intent 会把「正确拒绝」误判成漏放（实测已发生）。
            rec["rejected"] = status == "denied" or pred.get("intent") in (
                "out_of_scope", "need_clarify", "unsupported_metric")
        else:
            rec["rejected"] = None

        rows.append(rec)

    n = len(rows)
    restricted_rows = [r for r in rows if r["restricted"]]
    clarify_rows = [r for r in rows if r["need_clarify"]]

    def pct(x, d):
        return round(x / d, 4) if d else 0.0

    latencies.sort()
    # ★ 可执行率的分母必须是「应当出数」的用例：
    #   歧义题（应反问）、越权题（应拒绝）、问口径/超范围题本来就不产生 DSL，
    #   把它们算进分母会让这个指标永远上不去，且无法定位真正的生成失败。
    answerable = [(r, c) for r, c in zip(rows, golden)
                  if not c.get("requires_clarify")
                  and c.get("permission_expect") != "deny"
                  and c.get("intent") not in ("meta_question", "out_of_scope")]

    # ★ 意图准确率的分母要排除越权题：它们正确的行为是拒绝出数，
    #   golden 记的 intent（如 query_metric）是「用户想问什么」而非「系统该怎么答」，
    #   把正确拒绝算成意图错误会让这个指标无法解释。
    intent_rows = [(r, c) for r, c in zip(rows, golden)
                   if c.get("permission_expect") != "deny"]

    summary = {
        "engine": engine.name,
        "judge_mode": judge,
        "total": n,
        "answerable": len(answerable),
        "intent_scored": len(intent_rows),
        "intent_accuracy": pct(sum(1 for r, _ in intent_rows if r["intent_ok"]),
                               len(intent_rows)),
        "dsl_executable_rate": pct(
            sum(1 for r, _ in answerable if r["dsl_executable"]), len(answerable)),
        "result_correctness": pct(sum(1 for r in rows if r["correct"]), n),
        "clarify_quality": pct(sum(1 for r in clarify_rows if r["clarify_ok"]),
                               len(clarify_rows)),
        "false_ask_rate": pct(sum(1 for r in rows if r["false_ask"]), n),
        "reject_correctness": pct(sum(1 for r in restricted_rows if r["rejected"]),
                                  len(restricted_rows)),
        "leak_count": sum(1 for r in restricted_rows if not r["rejected"]),
        "p50_latency_ms": latencies[len(latencies) // 2] if latencies else 0,
        "p95_latency_ms": latencies[int(len(latencies) * 0.95) - 1] if latencies else 0,
    }

    # ★ 召回指标：必须与结果正确率并列展示，才能回答
    #   「这条失败是候选里没有正确指标（召回）还是模型选错（生成）」。
    #   口径与 eval_recall.py 一致：golden 的 metrics 全部落在候选集内才算命中。
    if registry:
        try:
            from recall import RecallIndex
            idx = RecallIndex(registry)
            rk = 8
            hit = tot = 0
            for c in golden:
                # out_of_scope 的用例本来就该答「暂不支持」，其 metrics 是"用户想问的"
                # 而非"应当召回的"，算进分母会把召回率永久压低（见 eval_recall.py 说明）
                if c.get("intent") == "out_of_scope":
                    continue
                gold = [m for m in (c.get("metrics") or []) if m]
                if not gold:
                    continue
                codes = [x["code"] for x in idx.search(c.get("question", ""), rk)]
                tot += 1
                if all(g in codes for g in gold):
                    hit += 1
            summary["recall_at_k"] = pct(hit, tot)
            summary["recall_k"] = rk
        except Exception as e:  # noqa: BLE001 —— 召回统计失败不能拖垮主评估
            summary["recall_at_k"] = None
            summary["recall_error"] = str(e)[:120]

    # 按难度/标签拆分，定位短板
    summary["by_difficulty"] = {}
    for d in ("easy", "medium", "hard"):
        sub = [r for r, c in zip(rows, golden) if c.get("difficulty") == d]
        if sub:
            summary["by_difficulty"][d] = pct(
                sum(1 for r in sub if r["correct"]), len(sub))

    return {"summary": summary, "rows": rows}


def main():
    ap = argparse.ArgumentParser(description="Golden 集评估执行器")
    ap.add_argument("--golden", required=True)
    ap.add_argument("--engine", default="mock_oracle",
                    choices=["mock_oracle", "mock_perturbed", "http"])
    ap.add_argument("--endpoint", help="http 引擎的问数接口地址")
    ap.add_argument("--registry", help="metric_registry.yaml")
    ap.add_argument("--db", help="数据库连接串（启用结果比对）")
    ap.add_argument("--today", default="2026-10-14")
    ap.add_argument("--output", help="输出 JSON 报告路径")
    ap.add_argument("--fail-fast", action="store_true",
                    help="越权漏放时以非零码退出")
    ap.add_argument("--gate", action="store_true",
                    help="CI 门禁：mock_oracle 自测未得满分时以非零码退出"
                         "（评估器自身坏了，后面的数字全都不可信）")
    args = ap.parse_args()

    ext = os.path.splitext(args.golden)[1].lower()
    if ext == ".jsonl":
        golden = [json.loads(l) for l in open(args.golden, encoding="utf-8")
                  if l.strip()]
    else:
        import csv
        golden = list(csv.DictReader(open(args.golden, encoding="utf-8-sig")))
        for g in golden:
            for k in ("metrics", "dimensions", "filters", "dept_scope", "tags"):
                if k in g and isinstance(g[k], str):
                    try:
                        g[k] = json.loads(g[k]) if g[k].strip() else []
                    except json.JSONDecodeError:
                        g[k] = []

    registry = None
    if args.registry:
        if yaml is None:
            raise SystemExit("需要 PyYAML: pip install pyyaml")
        registry = yaml.safe_load(open(args.registry, encoding="utf-8"))

    if args.engine == "mock_oracle":
        engine: BaseEngine = MockOracleEngine()
    elif args.engine == "mock_perturbed":
        engine = MockPerturbedEngine()
    else:
        if not args.endpoint:
            raise SystemExit("--engine http 需要 --endpoint")
        engine = HttpEngine(args.endpoint)

    report = evaluate(golden, engine, registry, args.db, args.today)
    s = report["summary"]

    print("=" * 68)
    print(f"评估引擎   : {s['engine']}")
    print(f"判定方式   : {s['judge_mode']}"
          f"{'（结果比对）' if s['judge_mode'] == 'result' else '（DSL 等价，非结果比对）'}")
    print(f"样本数     : {s['total']}")
    print("-" * 68)
    print(f"意图准确率      : {s['intent_accuracy']:.1%}")
    if s.get("recall_at_k") is not None:
        print(f"指标召回@{s.get('recall_k', 8):<3}: {s['recall_at_k']:.1%}"
              f"   ← 候选里有没有正确指标（失败归因用）")
    print(f"DSL 可执行率    : {s['dsl_executable_rate']:.1%}")
    print(f"结果正确率      : {s['result_correctness']:.1%}   ← 核心指标")
    print(f"反问得当率      : {s['clarify_quality']:.1%}")
    print(f"误反问率        : {s['false_ask_rate']:.1%}")
    print(f"越权拒绝正确率  : {s['reject_correctness']:.1%}   漏放 {s['leak_count']} 条")
    print(f"延迟 P50 / P95  : {s['p50_latency_ms']}ms / {s['p95_latency_ms']}ms")
    print(f"分难度正确率    : {s['by_difficulty']}")

    if s["engine"] == "mock_oracle":
        ok = (s["result_correctness"] == 1.0 and s["reject_correctness"] == 1.0
              and s["false_ask_rate"] == 0.0)
        print("-" * 68)
        print(f"评估器自测: {'[OK] 完美引擎得满分，评估逻辑可信' if ok else '[FAIL] 评估器有 bug'}")

    # 失败用例定位（真实评估时用于找短板）
    failed = [r for r in report["rows"] if not r["correct"]]
    if failed:
        print("-" * 68)
        print(f"失败用例 {len(failed)} 条（最多显示 10 条）:")
        for r in failed[:10]:
            print(f"  {r['id']} [{r['gold_intent']}->{r['pred_intent']}] "
                  f"{str(r['question'])[:38]}")
    if s["engine"] == "mock_perturbed":
        print("-" * 68)
        print("[INFO] 注入错误引擎：正确率应显著低于 100%，"
              "且越权漏放 > 0、误反问/漏反问 > 0，否则评估器失效")

    if args.output:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"[INFO] 报告已写入 {args.output}")

    if args.fail_fast and s["leak_count"] > 0:
        return 1
    if args.gate and s["engine"] == "mock_oracle":
        # 完美引擎必须拿满分。拿不到就是评估器/语义层被改坏了，不是"模型不行"，
        # 此时任何真实模型的分数都不可信 —— 所以这门禁必须拦在 CI 里。
        if not (s["result_correctness"] == 1.0 and s["reject_correctness"] == 1.0
                and s["false_ask_rate"] == 0.0):
            print("[GATE] mock_oracle 未得满分：评估器或语义层被改坏")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
