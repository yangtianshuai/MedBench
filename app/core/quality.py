# -*- coding: utf-8 -*-
"""数据质量分 Q（IN-03 的最小可用版）。

★ 先划清边界：这**不是**数据质量平台
    完整的 DQ 要做空值率、主键唯一性、值域合规、跨表一致、与 HIS 报表对账……
    那些都需要扫数据，代价是把 HIS 再压一遍 —— 而问数系统本身就在被限流保护。
    本模块只回答一个问题：**"这个数现在能不能信"，并且必须说得清理由。**

★ 分数只能由"已经观测到的事实"产生，不许猜：
    1) freshness  源域新鲜度（停更 / 延迟 / T+1 / 实时）+ 落后天数
    2) stability  近 7 天该指标的失败率、空结果率（来自审计，不是估算）
    3) governance 语义层自身是否完整（定义 / owner / 已发布）

★ 为什么把 governance 算进"质量分"：
    没有业务定义、没有责任人的指标，是**口径事故的温床**。
    它的数可能"对"，但没人能对它负责 —— 在医疗场景这就等于不可信。

输出约定：
    score ∈ [0,1]；level ∈ high/medium/low/unknown；reasons 逐条可读。
    任一硬门禁（停更、未发布）命中时直接压低，不做加权平均 —— 平均会把致命问题摊平。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

# 停更判定：最新数据落后超过这么多天，就算"不可作为现状使用"
STALE_DAYS = 45
# 稳定性评估窗口
STABILITY_DAYS = 7
# 样本太少时不下结论（宁可说"样本不足"，也不要给一个 0.5 的假精确分）
MIN_SAMPLES = 5

_FRESHNESS_BASE = {
    "streaming": 1.0,
    "t_plus_1": 0.85,
    "delayed": 0.45,
    "stale": 0.0,
}


def _days_since(date_str: Optional[str], today: str) -> Optional[int]:
    if not date_str:
        return None
    try:
        import datetime as dt
        d = dt.date.fromisoformat(str(date_str)[:10])
        t = dt.date.fromisoformat(str(today)[:10])
        return (t - d).days
    except Exception:  # noqa: BLE001
        return None


def _freshness_component(source_state: Optional[Dict[str, Any]], today: str):
    """返回 (分数或 None, 理由列表)。None = 不知道，不参与加权。"""
    if not source_state:
        return None, ["未登记数据源状态，无法评估新鲜度"]
    f = str(source_state.get("freshness") or "unknown")
    lag = _days_since(source_state.get("last_data_date"), today)
    reasons: List[str] = []

    if f == "stale":
        return 0.0, [f"源域「{source_state.get('domain')}」已停更"
                     f"（最新 {source_state.get('last_data_date')}）"]
    if f == "delayed":
        reasons.append(f"源域「{source_state.get('domain')}」同步延迟")
    if lag is not None and lag > STALE_DAYS:
        return 0.0, reasons + [f"最新数据落后 {lag} 天（>{STALE_DAYS} 天）"]

    base = _FRESHNESS_BASE.get(f)
    if base is None:
        return None, ["未登记数据源状态，无法评估新鲜度"]

    # 落后天数在基准分上再打折：T+1 且落后 5 天，显然比落后 1 天差
    if lag is not None and lag > 2:
        penalty = min(0.4, 0.08 * (lag - 2))
        base = max(0.0, base - penalty)
        reasons.append(f"最新数据落后 {lag} 天")
    if f == "t_plus_1":
        reasons.append("源为 T+1 同步，不含当日数据")
    return round(base, 3), reasons


def _stability_component(audit_rows: Optional[Iterable[Dict[str, Any]]],
                         metric: str, since: float):
    """失败率 / 空结果率来自审计行；样本不足则返回 None。"""
    if audit_rows is None:
        return None, []
    total = err = asked = empty = 0
    for r in audit_rows:
        if str(r.get("metric_code") or "") != metric:
            continue
        ts = r.get("ts")
        if ts is not None and float(ts) < since:
            continue
        total += 1
        st = str(r.get("status") or "")
        if st == "error":
            err += 1
        if st == "answered":
            asked += 1
            if int(r.get("row_count") or 0) == 0:
                empty += 1

    if total < MIN_SAMPLES:
        return None, [f"近 7 天仅 {total} 次调用，样本不足以下结论"]

    score = 1.0
    reasons: List[str] = []
    err_rate = err / total
    if err_rate > 0:
        score -= min(0.6, err_rate * 2)
        reasons.append(f"近 7 天失败率 {err_rate:.0%}")
    if asked:
        empty_rate = empty / asked
        if empty_rate >= 0.3:
            score -= min(0.3, empty_rate * 0.4)
            reasons.append(f"近 7 天 {empty_rate:.0%} 的查询结果为空（口径或数据可能有问题）")
    return round(max(0.0, score), 3), reasons


def _governance_component(meta: Dict[str, Any]):
    score = 1.0
    reasons: List[str] = []
    if str(meta.get("status") or "published") != "published":
        return 0.0, [f"指标状态为 {meta.get('status')}，未正式发布"]
    if not meta.get("business_definition"):
        score -= 0.4
        reasons.append("缺少业务定义（business_definition）")
    if not meta.get("owner"):
        score -= 0.3
        reasons.append("缺少责任科室（owner）")
    if not meta.get("notes"):
        score -= 0.2
        reasons.append("未标注易错点（notes）")
    if not meta.get("unit"):
        score -= 0.1
        reasons.append("缺少单位")
    return round(max(0.0, score), 3), reasons


def _level(score: float) -> str:
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def evaluate(meta: Dict[str, Any], source_state: Optional[Dict[str, Any]],
             audit_rows: Optional[Iterable[Dict[str, Any]]] = None,
             *, now: Optional[float] = None, today: Optional[str] = None) -> Dict[str, Any]:
    """纯函数：由"已观测到的事实"算 Q。便于单测，不依赖数据库。"""
    import datetime as dt

    now = time.time() if now is None else now
    today = today or dt.date.today().isoformat()
    code = str(meta.get("code") or "")

    fresh, fresh_why = _freshness_component(source_state, today)
    stab, stab_why = _stability_component(audit_rows, code, now - STABILITY_DAYS * 86400)
    gov, gov_why = _governance_component(meta)

    parts = [(0.5, fresh), (0.3, stab), (0.2, gov)]
    used = [(w, v) for w, v in parts if v is not None]
    wsum = sum(w for w, _ in used) or 1.0
    score = sum(w * v for w, v in used) / wsum

    # 硬门禁：未发布直接 0；停更封顶 0.2（其余项再好也不能当现状用）
    if gov == 0.0 and str(meta.get("status") or "published") != "published":
        score = 0.0
    if fresh == 0.0:
        score = min(score, 0.2)

    reasons = fresh_why + stab_why + gov_why
    if not reasons:
        reasons = ["新鲜度、稳定性、治理完备性均无异常"]

    return {
        "metric": code,
        "metric_name": meta.get("name"),
        "score": round(float(score), 3),
        "level": _level(score),
        "reasons": reasons,
        "components": {"freshness": fresh, "stability": stab, "governance": gov},
        "signal": "observed",     # 所有输入都是观测值，不是模型打分
    }


# ────────────────────────────────────────────────────────────
# 带缓存的取数封装（审计表读一次，批量评分共用）
# ────────────────────────────────────────────────────────────
_audit_cache: Dict[str, Any] = {}
_AUDIT_TTL = 60.0


def _audit_rows(limit: int = 2000) -> List[Dict[str, Any]]:
    hit = _audit_cache.get("rows")
    if hit and time.time() - hit[0] < _AUDIT_TTL:
        return hit[1]
    try:
        from app.core import meta_db
        rows = meta_db.list_audit(limit=limit)
    except Exception:  # noqa: BLE001 —— 审计读不到不影响出数，只是稳定性项缺失
        rows = []
    _audit_cache["rows"] = (time.time(), rows)
    return rows


def score_metric(code: str, registry: Optional[Dict[str, Any]] = None,
                 *, audit_rows: Optional[Iterable[Dict[str, Any]]] = None,
                 now: Optional[float] = None, today: Optional[str] = None) -> Dict[str, Any]:
    registry = registry or _registry()
    metas = {m.get("code"): m for m in (registry.get("metrics") or [])}
    meta = metas.get(code)
    if not meta:
        return {"metric": code, "score": 0.0, "level": "unknown",
                "reasons": ["指标未在语义层注册"], "components": {},
                "signal": "observed"}
    from app.core import meta_db
    state = meta_db.get_source_state(str(meta.get("domain") or ""))
    rows = _audit_rows() if audit_rows is None else audit_rows
    return evaluate(meta, state, rows, now=now, today=today)


def score_all(registry: Optional[Dict[str, Any]] = None,
              *, audit_rows: Optional[Iterable[Dict[str, Any]]] = None,
              now: Optional[float] = None, today: Optional[str] = None) -> List[Dict[str, Any]]:
    registry = registry or _registry()
    rows = _audit_rows() if audit_rows is None else audit_rows
    from app.core import meta_db
    out = []
    for m in (registry.get("metrics") or []):
        state = meta_db.get_source_state(str(m.get("domain") or ""))
        out.append(evaluate(m, state, rows, now=now, today=today))
    out.sort(key=lambda x: (x["score"], str(x["metric"])))
    return out


def _registry() -> Dict[str, Any]:
    from app.core.registry import load_registry
    return load_registry()


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    now = 1_800_000_000.0
    today = "2026-10-17"
    good = {"code": "m1", "name": "指标一", "unit": "人次", "owner": "医务处",
            "business_definition": "定义", "notes": "易错点", "status": "published"}
    streaming = {"domain": "门诊运营", "freshness": "streaming", "last_data_date": today}

    q = evaluate(good, streaming, [], now=now, today=today)
    check("治理完整 + 实时源 + 无审计样本 → 只按可得项算分",
          q["score"] >= 0.9 and q["components"]["stability"] is None, f"score={q['score']}")

    q = evaluate(good, {"domain": "x", "freshness": "stale", "last_data_date": "2024-10-31"},
                 [], now=now, today=today)
    check("源停更 → 封顶 0.2 且给出停更理由",
          q["score"] <= 0.2 and any("停更" in r for r in q["reasons"]), f"score={q['score']}")

    q = evaluate({"code": "m2", "name": "无治理", "status": "published"}, streaming, [],
                 now=now, today=today)
    check("缺定义/责任/易错点 → 治理项扣分并逐条说明",
          q["components"]["governance"] < 0.5
          and any("业务定义" in r for r in q["reasons"]), f"gov={q['components']['governance']}")

    q = evaluate({**good, "status": "draft"}, streaming, [], now=now, today=today)
    check("未发布 → 直接 0", q["score"] == 0.0 and q["level"] == "low")

    audits = [{"metric_code": "m1", "status": "error", "ts": now - 100} for _ in range(3)] + \
             [{"metric_code": "m1", "status": "answered", "row_count": 0, "ts": now - 100}
              for _ in range(3)]
    audits.append({"metric_code": "m1", "status": "answered", "row_count": 5, "ts": now - 100})
    q = evaluate(good, streaming, audits, now=now, today=today)
    check("失败率与空结果率会拉低稳定性分",
          q["components"]["stability"] < 0.8
          and any("失败率" in r for r in q["reasons"]), f"stab={q['components']['stability']}")

    q = evaluate(good, streaming, audits[:2], now=now, today=today)
    check("样本不足时稳定性项留空，不假装精确",
          q["components"]["stability"] is None
          and any("样本不足" in r for r in q["reasons"]))

    q = evaluate(good, {"domain": "x", "freshness": "t_plus_1",
                        "last_data_date": "2026-10-10"}, [], now=now, today=today)
    check("T+1 且落后多日 → 比正常 T+1 更低",
          q["components"]["freshness"] < 0.85, f"fresh={q['components']['freshness']}")

    q = evaluate(good, None, [], now=now, today=today)
    check("未登记源状态 → 不作假设（freshness 为空）",
          q["components"]["freshness"] is None
          and any("未登记数据源状态" in r for r in q["reasons"]))

    q = evaluate({"code": "m3", "name": "无审计"}, streaming, None, now=now, today=today)
    check("没有审计数据时不倒扣（缺证据 ≠ 有问题）", q["components"]["stability"] is None)
    return passed


if __name__ == "__main__":
    import sys
    ok = selftest()
    print("质量分自测: " + ("全部通过" if ok else "存在失败"))
    sys.exit(0 if ok else 1)
