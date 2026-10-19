#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Few-shot 示例选择与渲染（G2 消融的实验条件）。

★ 为什么不是「把示例库全塞进提示词」：
   80 条示例 ≈ 上万 token，既贵又稀释注意力；更重要的是**会诱导抄袭** ——
   模型倾向于复用最近出现过的指标 code，而示例里的 code 往往不在本轮候选里，
   输出的 DSL 完全合法、校验层拦不住，只是指标选错了。
   所以：动态选 3~5 条 + 结构示例不含真实 code + 口径示例必须命中候选。

★ 两类示例，职责不同：
   structure —— 只演示 DSL 形状（`<指标code>` 占位），零抄袭风险
   caliber   —— 真实 code，只在「它的指标已进候选」时注入，用来看口径约定

★ 选择策略（可解释，选中理由会写进 trace）：
   1. 结构示例固定 2 条：优先 intent 相同的
   2. 口径示例按 intent 命中(+3) / 维度命中(+2) / 问句相似(+0~2) / 指标在当前候选(+1) 排序
   3. 总条数受 max_examples 限制

用法:
    python fewshot.py --selftest
    python fewshot.py --show "各科室平均住院日排名前10"
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Set

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_PATH = os.path.join(_HERE, "..", "semantic-layer", "examples.yaml")
_DEFAULT_REGISTRY = os.path.join(_HERE, "..", "semantic-layer", "metric_registry.yaml")

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_cache: Dict[str, Any] = {}


def load_examples(path: Optional[str] = None) -> Dict[str, Any]:
    """加载 examples.yaml（按 mtime 缓存）。文件缺失时返回空库，不影响主链路。"""
    path = os.path.abspath(path or os.environ.get("ASK_EXAMPLES") or _DEFAULT_PATH)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"examples": [], "policy": {}, "_source": None}
    hit = _cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]
    if yaml is None:
        return {"examples": [], "policy": {}, "_source": None}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 —— 示例库坏了不能拖垮问数
        data = {}
    data.setdefault("examples", [])
    data["_source"] = path
    _cache[path] = (mtime, data)
    return data


def _bigrams(s: str) -> Set[str]:
    s = "".join(str(s or "").split())
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _overlap(a: str, b: str) -> float:
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / min(len(ba), len(bb))


class ExampleIndex:
    """示例选择器。一次构建，多次查询。"""

    def __init__(self, data: Optional[Dict[str, Any]] = None,
                 registry: Optional[Dict[str, Any]] = None):
        data = data if isinstance(data, dict) else load_examples()
        self.policy = data.get("policy") or {}
        self.examples = [e for e in (data.get("examples") or [])
                         if isinstance(e, dict) and e.get("id")]
        self.max_examples = int(self.policy.get("max_examples") or 4)
        self.n_structure = int(self.policy.get("structure") or 2)
        self.max_caliber = int(self.policy.get("max_caliber") or 3)
        self.metric_codes = {m.get("code") for m in ((registry or {}).get("metrics") or [])}

    # ── 选择 ──────────────────────────────────────────────
    def select(self, question: str, candidates: Sequence[Dict[str, Any]],
               intent_hint: Optional[str] = None,
               dims: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
        """返回选中的示例（带 _why 字段，便于 trace 排查"为什么给了这条"）。"""
        cand_codes = {c.get("code") for c in (candidates or []) if c.get("code")}
        dims = set(dims or [])
        picked: List[Dict[str, Any]] = []

        # 1) 结构示例：intent 相同的优先
        structure = [e for e in self.examples if e.get("kind") == "structure"]
        structure.sort(key=lambda e: (0 if (e.get("dsl") or {}).get("intent") == intent_hint
                                      else 1, e.get("id")))
        for e in structure[:self.n_structure]:
            picked.append(dict(e, _why="structure"))

        # 2) 口径示例：指标必须在候选里（防止诱导抄袭）
        scored = []
        for e in self.examples:
            if e.get("kind") != "caliber":
                continue
            dsl = e.get("dsl") or {}
            ms = [m for m in (dsl.get("metrics") or [])]
            if not ms or not set(ms).issubset(cand_codes):
                continue
            score = 0.0
            if dsl.get("intent") == intent_hint:
                score += 3.0
            score += 2.0 * len(dims & set(dsl.get("dimensions") or []))
            score += 2.0 * _overlap(e.get("question") or "", question)
            if ms and ms[0] in {c.get("code") for c in (candidates or [])[:3]}:
                score += 1.0
            scored.append((score, str(e.get("id")), e))
        scored.sort(key=lambda x: (-x[0], x[1]))
        for score, _id, e in scored[:self.max_caliber]:
            picked.append(dict(e, _why=f"caliber(score={score:.1f})"))

        return picked[:self.max_examples]

    # ── 渲染 ──────────────────────────────────────────────
    @staticmethod
    def render(selected: Sequence[Dict[str, Any]]) -> str:
        if not selected:
            return ""
        lines = [
            "\n【参考示例】（只学「写法」与「口径约定」：",
            " ① 指标只能取【候选指标】里的 code，示例里没在候选中的 code 一律不得使用；",
            " ② 结构示例中的 <指标code> / <科室码> 是占位符，禁止原样输出；",
            " ③ 时间锚点、过滤写法、对比方式照示例来，不要自创字段。\n",
        ]
        for i, e in enumerate(selected, 1):
            dsl = json.dumps(e.get("dsl") or {}, ensure_ascii=False,
                             separators=(",", ":"))
            kind = "格式" if e.get("kind") == "structure" else "口径"
            lines.append(f"{i}. [{kind}] 问：{e.get('question')}")
            lines.append(f"   DSL：{dsl}")
        return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def selftest(examples_path: Optional[str] = None,
             registry_path: Optional[str] = None) -> List[str]:
    """示例库自身的回归：合法性、防抄袭、选择策略。"""
    from dsl import dsl_from_dict, validate_dsl  # noqa: E402  （同目录模块）

    if yaml is None:
        return ["需要 PyYAML"]
    data = load_examples(examples_path or _DEFAULT_PATH)
    registry = yaml.safe_load(open(registry_path or _DEFAULT_REGISTRY, encoding="utf-8"))
    idx = ExampleIndex(data, registry)
    fails: List[str] = []
    codes = {m.get("code") for m in (registry.get("metrics") or [])}

    if not idx.examples:
        return ["示例库为空"]

    # ① 每条口径示例都必须能通过语义层校验（否则模型学到的是非法 DSL）
    for e in idx.examples:
        if e.get("kind") != "caliber":
            continue
        dsl = e.get("dsl") or {}
        ms = dsl.get("metrics") or []
        missing = [m for m in ms if m not in codes]
        if missing:
            fails.append(f"[未注册指标] {e['id']} → {missing}")
            continue
        try:
            errs = validate_dsl(dsl_from_dict(dsl, registry), registry)
        except Exception as ex:  # noqa: BLE001
            fails.append(f"[DSL 无法解析] {e['id']}：{ex}")
            continue
        if errs:
            fails.append(f"[非法 DSL] {e['id']}：{'; '.join(errs)[:100]}")

    # ② 结构示例绝不能出现真实指标 code（防抄袭）
    for e in idx.examples:
        if e.get("kind") != "structure":
            continue
        leaked = [m for m in ((e.get("dsl") or {}).get("metrics") or []) if m in codes]
        if leaked:
            fails.append(f"[样例泄漏] {e['id']} 结构示例里出现真实指标 {leaked}")

    # ③ 选择策略：候选命中才注入口径示例
    cand_hit = [{"code": "ip_los_avg", "name": "平均住院日"}]
    got_hit = idx.select("各科室平均住院日排名前10", cand_hit, "rank", ["dept"])
    if not any(e["id"] == "EX-C06" for e in got_hit):
        fails.append("[选择] 候选含 ip_los_avg 时应注入 EX-C06")
    got_miss = idx.select("各科室平均住院日排名前10", [{"code": "op_visit_cnt"}], "rank", ["dept"])
    if any(e["id"] == "EX-C06" for e in got_miss):
        fails.append("[选择] 候选不含 ip_los_avg 时禁止注入 EX-C06（防抄袭）")
    if len(got_hit) > idx.max_examples:
        fails.append(f"[选择] 超出 max_examples={idx.max_examples}")

    # ④ 结构示例优先匹配 intent
    got_rank = idx.select("各科室出院人次排名前10", [], "rank", ["dept"])
    if not any(e["id"] == "EX-S01" for e in got_rank):
        fails.append("[选择] rank 意图应优先注入排名结构示例 EX-S01")

    # ⑤ 渲染出的块必须带防抄袭提示
    block = idx.render(got_hit)
    if "不得使用" not in block or "占位符" not in block:
        fails.append("[渲染] 示例块缺少防抄袭/占位符提示")

    return fails


def audit_golden(golden_path: str, examples_path: Optional[str] = None,
                 registry_path: Optional[str] = None) -> Dict[str, Any]:
    """在 golden 集上审计「示例库到底有没有被用上」。

    为什么需要：示例库最容易变成"配置了但从不生效"的死资产 ——
    口径示例要求指标在候选里，候选一旦没召回，示例就永远不会被选中。
    这个审计把「注入率 / 平均条数 / 只能给结构示例的题」直接摊开。
    """
    from pipeline import (predict_dimensions, predict_intent,  # noqa: E402
                          recall_metrics)

    if yaml is None:
        return {"error": "需要 PyYAML"}
    registry = yaml.safe_load(open(registry_path or _DEFAULT_REGISTRY, encoding="utf-8"))
    idx = ExampleIndex(load_examples(examples_path), registry)
    cases = [json.loads(l) for l in open(golden_path, encoding="utf-8") if l.strip()]
    # out_of_scope 的用例本来就该答「暂不支持」，示例库不给它口径示例是正确的，
    # 不能算成"覆盖不到"（否则每次审计都会挂着一条假缺口）。
    unsupported = [c for c in cases
                   if c.get("metrics") and c.get("intent") == "out_of_scope"]
    cases = [c for c in cases
             if c.get("metrics") and c.get("intent") != "out_of_scope"]

    rows = []
    for c in cases:
        q = c.get("question") or ""
        cands = recall_metrics(q, registry)
        sel = idx.select(q, cands, predict_intent(q), predict_dimensions(q))
        n_cal = sum(1 for e in sel if e.get("kind") == "caliber")
        rows.append({"id": c.get("id"), "question": q, "n": len(sel),
                     "caliber": n_cal, "ids": [e.get("id") for e in sel]})
    n = len(rows)
    return {
        "total": n,
        "avg_selected": round(sum(r["n"] for r in rows) / n, 2) if n else 0.0,
        "max_selected": max((r["n"] for r in rows), default=0),
        "caliber_hit_rate": round(sum(1 for r in rows if r["caliber"]) / n, 4) if n else 0.0,
        "structure_only": [r for r in rows if not r["caliber"]],
        "excluded_unsupported": [
            {"id": c.get("id"), "question": c.get("question")} for c in unsupported],
        "rows": rows,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Few-shot 示例选择")
    ap.add_argument("--examples", default=_DEFAULT_PATH)
    ap.add_argument("--registry", default=_DEFAULT_REGISTRY)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--audit-golden", help="在 golden 集上审计示例注入情况")
    ap.add_argument("--show", help="打印某个问题的示例块")
    ap.add_argument("--candidates", default="", help="逗号分隔的候选指标 code")
    args = ap.parse_args(argv)

    if args.selftest:
        fails = selftest(args.examples, args.registry)
        for f in fails:
            print(f"[FAIL] {f}")
        print(f"示例库自测: {'全部通过' if not fails else f'{len(fails)} 条失败'}")
        return 1 if fails else 0

    if args.audit_golden:
        rep = audit_golden(args.audit_golden, args.examples, args.registry)
        if rep.get("error"):
            print(rep["error"])
            return 2
        print("=" * 68)
        print(f"用例数            : {rep['total']}")
        print(f"平均注入条数      : {rep['avg_selected']}"
              f"（上限 {load_examples(args.examples).get('policy', {}).get('max_examples')}）")
        print(f"最多注入条数      : {rep['max_selected']}")
        print(f"含口径示例的比例  : {rep['caliber_hit_rate']:.1%}")
        so = rep["structure_only"]
        if so:
            print(f"\n只有结构示例的用例 {len(so)} 条（示例库覆盖不到，按需补）：")
            for r in so[:15]:
                print(f"  {r['id']}  {r['question']}")
        else:
            print("所有用例都能拿到至少 1 条口径示例")
        if rep.get("excluded_unsupported"):
            print(f"\n已排除 {len(rep['excluded_unsupported'])} 条语义层明确不支持的用例：")
            for r in rep["excluded_unsupported"]:
                print(f"  {r['id']}  {r['question']}")
        return 0

    if yaml is None:
        print("需要 PyYAML")
        return 2
    registry = yaml.safe_load(open(args.registry, encoding="utf-8"))
    idx = ExampleIndex(load_examples(args.examples), registry)
    cands = [{"code": c.strip()} for c in args.candidates.split(",") if c.strip()]
    if not cands:
        sys.path.insert(0, _HERE)
        from pipeline import predict_dimensions, predict_intent, recall_metrics
        cands = recall_metrics(args.show or "", registry)
        sel = idx.select(args.show or "", cands,
                         predict_intent(args.show or ""),
                         predict_dimensions(args.show or ""))
    else:
        sel = idx.select(args.show or "", cands)
    print(f"候选：{[c['code'] for c in cands]}")
    print(idx.render(sel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
