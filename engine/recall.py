#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指标召回（候选收缩）。

★ 职责边界：
    召回只做 **narrowing**，不做指标认定 —— 认定权在 LLM，否决权在语义层。
    本模块即使召回错了，也只能让候选变差，不能让口径变错。

★ 为什么不用纯向量检索：
    中文医疗指标名短、别名多，字面 + 主题域已经能覆盖绝大多数问法，
    且每条候选都能解释「为什么进来」（见 explain()）。
    向量模型的部署成本与不可解释性，在院内评审时是负担。
    指标数突破 200 后再在 search() 之后接一路向量召回做 RRF 融合即可，
    接口（返回 [{"code": ...}]）不用改。

★ 打分三路（可解释、可调参）：
    1. 整词命中  100 + 3×词长   —— 名称/别名/关键词完整出现在问题里（最可靠）
    2. 碎片命中  45 × 覆盖率²    —— 别名被问题拆开（"门诊多少人次" vs "门诊人次"）
    3. 主题域    20              —— 命中「住院/收入/手术」等域词，保证同域指标进候选

用法:
    python recall.py --registry ../semantic-layer/metric_registry.yaml \\
        --explain "心内科上个月收入多少"
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_SYNONYMS = os.path.join(_HERE, "..", "semantic-layer", "synonyms.yaml")

EXACT_BASE = 100.0
EXACT_LEN_BONUS = 3.0
PARTIAL_MAX = 45.0
DOMAIN_SCORE = 20.0
MIN_PARTIAL_COVERAGE = 0.5
# 兜底默认关闭：空候选是「超出语义层范围」的有效信号。
# 实测反例（G0016「床位使用率」）：该指标尚未注册，兜底会把 8 个无关指标塞给模型，
# 模型于是拿「占用床日数」冒充「床位使用率」——把「答不了」变成「答错」，这比空集更糟。
FALLBACK_MAX_METRICS = 0

# 内置兜底词表：synonyms.yaml 缺失/加载失败时仍能工作（宁可退化，不可崩溃）
_BUILTIN_EQUIV = [
    ("病人", "患者"), ("病号", "患者"), ("例次", "人次"),
    ("营收", "收入"), ("花费", "费用"), ("医生", "医师"), ("大夫", "医师"),
]
_BUILTIN_DOMAINS = {
    "门诊运营": ["门诊", "门急诊", "挂号"],
    "住院运营": ["住院", "出院", "入院", "在院", "收治"],
    "收入": ["收入", "费用", "钱", "营收"],
    "手术": ["手术", "台次"],
    "医保DRG": ["DRG", "drg", "医保", "权重", "CMI", "cmi"],
    "医疗质控": ["质控", "死亡", "再入院", "重返", "质量"],
}

_syn_cache: Dict[str, Any] = {}


# ────────────────────────────────────────────────────────────
# 文本归一化
# ────────────────────────────────────────────────────────────
def _base_norm(s: Any) -> str:
    """全角→半角、转小写、去空白与标点。CJK 属于 \\w，不会被误删。"""
    s = unicodedata.normalize("NFKC", str(s or "")).lower()
    return re.sub(r"[\s\W_]+", "", s)


def _bigrams(s: str) -> Set[str]:
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i:i + 2] for i in range(len(s) - 1)}


def load_synonyms(path: Optional[str] = None) -> Dict[str, Any]:
    """加载 synonyms.yaml（按 mtime 缓存）。失败时返回内置词表，不抛异常。"""
    path = (path or os.environ.get("ASK_SYNONYMS")
            or os.environ.get("SEMANTIC_SYNONYMS") or _DEFAULT_SYNONYMS)
    path = os.path.abspath(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"equivalences": [], "domains": dict(_BUILTIN_DOMAINS),
                "fallback_max_metrics": FALLBACK_MAX_METRICS, "_source": "builtin"}

    hit = _syn_cache.get(path)
    if hit and hit[0] == mtime:
        return hit[1]

    if yaml is None:
        return {"equivalences": [], "domains": dict(_BUILTIN_DOMAINS),
                "fallback_max_metrics": FALLBACK_MAX_METRICS, "_source": "builtin"}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001 —— 词表损坏不能拖垮问数主链路
        data = {}
    data.setdefault("domains", {})
    data["_source"] = path
    _syn_cache[path] = (mtime, data)
    return data


class RecallIndex:
    """指标召回索引。一次构建，多次查询（构建成本 O(指标数)，20 个指标可忽略）。"""

    def __init__(self, registry: Dict[str, Any],
                 synonyms: Optional[Dict[str, Any]] = None,
                 warn: Optional[Any] = None):
        self.registry = registry or {}
        self.warn = warn or (lambda msg: None)
        syn = synonyms if isinstance(synonyms, dict) else load_synonyms()

        # 等价词表：长词优先替换，避免「病人数」被「病人」截断
        pairs: List[Tuple[str, str]] = []
        for item in (syn.get("equivalences") or []):
            canonical = _base_norm(item.get("canonical"))
            if not canonical:
                continue
            for form in (item.get("forms") or []):
                f = _base_norm(form)
                if f and f != canonical:
                    pairs.append((f, canonical))
        if not pairs:
            pairs = list(_BUILTIN_EQUIV)
        self._equiv = sorted(pairs, key=lambda x: -len(x[0]))

        self.metrics: List[Dict[str, Any]] = [
            m for m in (self.registry.get("metrics") or []) if m.get("code")]
        self._domain_of: Dict[str, str] = {
            m["code"]: str(m.get("domain") or "") for m in self.metrics}

        # 每个指标的归一化表面形式（名称 + 别名 + 关键词）
        self._surfaces: Dict[str, List[str]] = {}
        for m in self.metrics:
            forms: List[str] = [m.get("name") or ""]
            forms += list(m.get("alias") or [])
            forms += list(m.get("keywords") or [])
            normed: List[str] = []
            for f in forms:
                n = self.norm(f)
                if len(n) >= 2 and n not in normed:
                    normed.append(n)
            self._surfaces[m["code"]] = normed

        # 主题域词表：词 → 该域下的指标集合
        domains_cfg = syn.get("domains") or _BUILTIN_DOMAINS
        actual_domains = set(v for v in self._domain_of.values() if v)
        self._domain_terms: List[Tuple[str, Set[str]]] = []
        for domain, terms in domains_cfg.items():
            if domain not in actual_domains:
                self.warn(f"synonyms.yaml 中的主题域「{domain}」在语义层不存在，已忽略")
                continue
            codes = {c for c, d in self._domain_of.items() if d == domain}
            for t in (terms or []):
                tn = self.norm(t)
                if tn:
                    self._domain_terms.append((tn, codes))
        raw_fb = syn.get("fallback_max_metrics", FALLBACK_MAX_METRICS)
        try:
            self._fallback_max = int(raw_fb)
        except (TypeError, ValueError):
            self._fallback_max = FALLBACK_MAX_METRICS

    # ── 归一化（含同义词替换） ──────────────────────────────
    def norm(self, s: Any) -> str:
        out = _base_norm(s)
        if not out:
            return out
        for form, canonical in self._equiv:
            if form in out:
                out = out.replace(form, canonical)
        return out

    # ── 打分 ───────────────────────────────────────────────
    def _surface_score(self, sf: str, q: str, q_bg: Set[str]) -> Tuple[float, str]:
        if sf in q:
            return EXACT_BASE + EXACT_LEN_BONUS * min(len(sf), 12), "exact"
        if len(sf) < 3:
            return 0.0, ""
        sfb = _bigrams(sf)
        if not sfb:
            return 0.0, ""
        inter = len(sfb & q_bg)
        cov = inter / len(sfb)
        if cov < MIN_PARTIAL_COVERAGE:
            return 0.0, ""
        return PARTIAL_MAX * cov * cov, "partial"

    def explain(self, question: str, top_k: int = 8) -> List[Dict[str, Any]]:
        q = self.norm(question)
        if not q or not self.metrics:
            return []
        q_bg = _bigrams(q)

        matched_domains: Set[str] = set()
        for term, codes in self._domain_terms:
            if term in q:
                matched_domains |= {self._domain_of[c] for c in codes}

        rows: List[Dict[str, Any]] = []
        for m in self.metrics:
            code = m["code"]
            best, best_sf, best_kind = 0.0, "", ""
            for sf in self._surfaces.get(code, []):
                s, kind = self._surface_score(sf, q, q_bg)
                if s > best:
                    best, best_sf, best_kind = s, sf, kind
            domain_hit = self._domain_of.get(code) in matched_domains
            total = best + (DOMAIN_SCORE if domain_hit else 0.0)
            if total <= 0:
                continue
            rows.append({
                "code": code, "name": m.get("name"), "score": round(total, 1),
                "why": {"surface": best_sf or None, "kind": best_kind or None,
                        "domain": self._domain_of.get(code) if domain_hit else None},
                "metric": m,
            })

        rows.sort(key=lambda r: (-r["score"], r["code"]))
        out = rows[:top_k]

        # 兜底默认关闭（fallback_max_metrics: 0）。开启时仅在指标总数很少的场景使用，
        # 代价是「超出范围」会被稀释成「大概能答」——默认不允许。
        if not out and 0 < len(self.metrics) <= self._fallback_max:
            out = [{"code": m["code"], "name": m.get("name"), "score": 0.0,
                    "why": {"surface": None, "kind": "fallback", "domain": None},
                    "metric": m} for m in self.metrics[:top_k]]
        return out

    def search(self, question: str, top_k: int = 8) -> List[Dict[str, Any]]:
        """返回候选指标（与旧 recall_metrics 输出结构一致，可直接喂提示词）。"""
        return [{
            "code": r["code"],
            "name": r["name"],
            "score": r["score"],
            "supported_date_basis": r["metric"].get("supported_date_basis")
            or r["metric"].get("date_basis"),
            "supported_dept_basis": r["metric"].get("supported_dept_basis")
            or r["metric"].get("dept_basis"),
            "sensitivity": r["metric"].get("sensitivity", "public"),
        } for r in self.explain(question, top_k)]


def recall_metrics(question: str, registry: Dict[str, Any],
                   top_k: int = 8) -> List[Dict[str, Any]]:
    """便捷函数（无状态，每次重建索引；批量场景请复用 RecallIndex）。"""
    return RecallIndex(registry).search(question, top_k)


# ────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────
def selftest(registry: Dict[str, Any]) -> List[str]:
    """固化三类历史失败的回归：断词、口语泛化、同义词。

    每条断言都对应一个真实踩过的坑，改打分公式后必须先跑这个。
    """
    fails: List[str] = []
    idx = RecallIndex(registry)
    codes = {m["code"] for m in idx.metrics}

    def top(q: str, n: int = 8) -> List[str]:
        return [r["code"] for r in idx.search(q, n)]

    cases = [
        # (问题, 期望进入候选的指标, 说明)
        ("去年同期门诊多少人次", "op_visit_cnt", "别名被拆开：门诊…人次"),
        ("医保患者上个季度出院多少人次", "ip_discharge_cnt", "别名被拆开：出院…人次"),
        ("上个月住院多少患者", "ip_discharge_cnt", "口语：「住院多少患者」"),
        ("心内科上个月收入多少", "op_revenue", "泛化：「收入」→ 同域指标"),
        ("上个月全院收入怎么样", "ip_revenue", "泛化：「收入」→ 同域指标"),
        ("药占比和耗占比分别是多少", "material_rate", "多指标并列"),
        ("四级手术占比多少", "surgery_level4_rate", "长别名不被长度惩罚压死"),
    ]
    for q, expect, why in cases:
        got = top(q)
        if expect not in got:
            fails.append(f"[召回缺失] {q} → 期望 {expect}，实得 {got}（{why}）")

    # 同义词归一：两侧都要归一，否则「出院病人」对不上「出院患者」
    if idx.norm("出院病人数") != idx.norm("出院患者数"):
        fails.append("[归一化] 「病人」未等价到「患者」")

    # 精确命中必须排第一，不能被域词稀释
    for q, expect in [("上个月门诊收入多少", "op_revenue"),
                      ("各科室平均住院日排名前10", "ip_los_avg")]:
        got = top(q)
        if not got or got[0] != expect:
            fails.append(f"[排序] {q} → 首名应为 {expect}，实得 {got[:3]}")

    # 超出语义层范围时返回空，而不是塞无关指标（防止「答不了」变「答错」）
    bed_registered = any("使用率" in str(m.get("name")) for m in idx.metrics)
    if not bed_registered and top("全院床位使用率现在多少"):
        fails.append("[兜底] 未注册指标的问题不应返回候选（空集是有效信号）")

    return fails


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="指标召回调试")
    ap.add_argument("--registry", default=os.path.join(
        _HERE, "..", "semantic-layer", "metric_registry.yaml"))
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--explain", help="待排查的问句")
    ap.add_argument("--selftest", action="store_true", help="跑召回回归自测")
    args = ap.parse_args(argv)

    if yaml is None:
        print("需要 PyYAML: pip install pyyaml")
        return 2
    registry = yaml.safe_load(open(args.registry, encoding="utf-8"))

    if args.selftest:
        fails = selftest(registry)
        for f in fails:
            print(f"[FAIL] {f}")
        print(f"召回自测: {'全部通过' if not fails else f'{len(fails)} 条失败'}")
        return 1 if fails else 0

    warns: List[str] = []
    idx = RecallIndex(registry, warn=warns.append)
    for w in warns:
        print(f"[WARN] {w}")

    for q in ([args.explain] if args.explain else []):
        print(f"问题: {q}")
        print(f"归一化: {idx.norm(q)}")
        for r in idx.explain(q, args.top_k):
            why = r["why"]
            print(f"  {r['score']:>6.1f}  {r['code']:<28} "
                  f"[{why['kind'] or '-'}] {why['surface'] or ''} "
                  f"{('域=' + why['domain']) if why['domain'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
