# -*- coding: utf-8 -*-
"""结构-价格两因素归因的取数与编排（IN-04 接线层）。

分工：
    engine/attribution.py  纯数学：分组两期数据 → 结构/价格/交互/组别增减（含恒等式自检）
    本模块                 取数：把「指标 × 维度 × 两期」翻译成**语义层 DSL**，
                           复用同一条受限生成链路取回分组数据，再交给引擎分解

★ 为什么取数不自己写 SQL：
    自己拼 SQL 会绕过 RLS、成熟度过滤、小样本抑制、口径写回 —— 归因就成了一条
    "看起来有数、其实没护栏"的旁路。这里两条查询都走 `AskPipeline`，
    护栏与主链路完全一致；归因只在**数学层**上新增能力。

★ 为什么不用 engine_bridge.ask：
    那个入口会为每次取数落一条审计 + 一份快照。一次归因 = 两条内层查询，
    会在审计里长成"用户问了两个问题"，也污染采纳率统计。
    这里直接调管线，由外层动作统一留痕。
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "engine"))

from attribution import (  # noqa: E402
    DecompositionError, GroupRow, decompose, render,)


class AttributionError(ValueError):
    """输入/前置条件不满足；消息直接面向用户。"""


# ────────────────────────────────────────────────────────────
# 时间区间
# ────────────────────────────────────────────────────────────
def _month_span(d: dt.date) -> Tuple[dt.date, dt.date]:
    """返回 [当月首日, 次月首日)。"""
    start = d.replace(day=1)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    return start, end


def _last_complete_month(today: dt.date) -> dt.date:
    return _month_span(today.replace(day=1) - dt.timedelta(days=1))[0]


def periods(period: str, today: dt.date) -> Dict[str, Tuple[dt.date, dt.date]]:
    """当期 vs 基期。mom = 上个月对再上个月；yoy = 上个月对去年同月。"""
    cur_start = _last_complete_month(today)
    cur = _month_span(cur_start)
    if period == "yoy":
        base_start = cur_start.replace(year=cur_start.year - 1)
    elif period == "mom":
        base_start = _month_span(cur_start - dt.timedelta(days=1))[0]
    else:
        raise AttributionError(f"不支持对比方式 '{period}'，只支持 mom / yoy")
    return {"base": _month_span(base_start), "current": cur}


# ────────────────────────────────────────────────────────────
# 取数
# ────────────────────────────────────────────────────────────
def _metric_meta(registry: Dict[str, Any], metric: str) -> Dict[str, Any]:
    for m in registry.get("metrics") or []:
        if m.get("code") == metric:
            return m
    raise AttributionError(f"指标 '{metric}' 未在语义层注册")


def _measures(registry: Dict[str, Any]) -> Dict[str, Any]:
    return {m.get("code"): m for m in (registry.get("measures") or [])}


def _is_per_case(meta: Dict[str, Any], measures: Dict[str, Any]) -> bool:
    """指标值的语义：例均水平（费用/床日 per case）还是总量（收入合计）。"""
    if meta.get("type") == "ratio":
        return True
    measure = measures.get(meta.get("measure")) or {}
    agg = str(measure.get("agg") or "")
    if agg in ("count", "count_distinct"):
        # 人次/例数类指标做分解没有意义（各组例均恒为 1），直接拒绝
        raise AttributionError(
            f"指标「{meta.get('name') or meta.get('code')}」是人次/例数类，"
            "结构-价格分解对它没有意义。请换用水平型指标（次均费用、平均住院日）"
            "或总量型指标（收入）。")
    return agg == "avg"


def _fetch(pipe, metric: str, dimension: str, span: Tuple[dt.date, dt.date],
           span_label: str, *, dept_basis: Optional[str],
           date_basis: Optional[str], role: str,
           dept_scope, campus_scope) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    start, end = span
    dsl = {
        "intent": "query_metric",
        "metrics": [metric],
        "dimensions": [dimension],
        "filters": [],
        "time": {
            "granularity": "month",
            "range": {"type": "absolute",
                      "start": start.isoformat(),
                      "end": (end - dt.timedelta(days=1)).isoformat()},
            "date_basis": date_basis,
            "compare": "none",
        },
        "options": {"dept_basis": dept_basis},
        "confidence": 0.99,
    }
    res = pipe.ask(f"[attribution:{span_label}] {metric} × {dimension}",
                   role=role, dept_scope=tuple(dept_scope or ()),
                   campus_scope=campus_scope, inject_dsl=dsl)
    if res.status != "answered" or res.rows is None:
        msg = (res.answer or "取数失败").splitlines()[0]
        raise AttributionError(f"{span_label}取数未完成：{msg}")
    return res.rows, res.sql


def _to_group_rows(base_rows: List[Dict[str, Any]], cur_rows: List[Dict[str, Any]],
                   metric: str, group_col: str, per_case: bool,
                   names: Dict[str, str]) -> List[GroupRow]:
    def index(rows):
        out: Dict[str, Tuple[float, float]] = {}
        for r in rows:
            g = r.get(group_col)
            v = r.get(metric)
            if g is None or v is None:
                continue
            cnt = float(r.get("_sample_size") or 0)
            if cnt <= 0:
                continue
            amt = float(v) * cnt if per_case else float(v)
            out[str(g)] = (cnt, amt)
        return out

    b, c = index(base_rows), index(cur_rows)
    rows: List[GroupRow] = []
    for g in sorted(set(b) | set(c)):
        c0, a0 = b.get(g, (0.0, 0.0))
        c1, a1 = c.get(g, (0.0, 0.0))
        rows.append(GroupRow(group=g, name=names.get(g, g),
                             count_0=c0, amount_0=a0,
                             count_1=c1, amount_1=a1))
    return rows


def _level_name(meta: Dict[str, Any]) -> str:
    unit = str(meta.get("unit") or "")
    return {"元": "费用", "天": "住院日", "床日": "床日"}.get(unit, "例均")


# ────────────────────────────────────────────────────────────
# 对外入口
# ────────────────────────────────────────────────────────────
def run(*, metric: str = "ip_avg_fee", dimension: str = "dept",
        period: str = "mom", role: str, dept_scope,
        campus_scope=None, date_basis: Optional[str] = None,
        dept_basis: Optional[str] = None,
        min_group_size: Optional[int] = None,
        today: Optional[dt.date] = None,
        registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """跑一次结构-价格分解，返回可直接给前端/解读用的结构。

    抛 AttributionError（输入/前置问题）或 DecompositionError（引擎拒绝出结论）。
    """
    from app.core import engine_bridge

    registry = registry or engine_bridge.load_registry()
    meta = _metric_meta(registry, metric)
    measures = _measures(registry)
    per_case = _is_per_case(meta, measures)

    dims = {d.get("code"): d for d in (registry.get("dimensions") or [])}
    dim = dims.get(dimension)
    if not dim:
        raise AttributionError(f"维度 '{dimension}' 未在语义层注册")
    if dimension not in (meta.get("dimensions") or []):
        raise AttributionError(
            f"指标「{meta.get('name')}」不支持按「{dim.get('name') or dimension}」分解")

    today = today or dt.date.today()
    spans = periods(period, today)
    pipe = engine_bridge.get_pipeline()

    base_rows, base_sql = _fetch(pipe, metric, dimension, spans["base"], "基期",
                                 dept_basis=dept_basis, date_basis=date_basis,
                                 role=role, dept_scope=dept_scope,
                                 campus_scope=campus_scope)
    cur_rows, cur_sql = _fetch(pipe, metric, dimension, spans["current"], "报告期",
                               dept_basis=dept_basis, date_basis=date_basis,
                               role=role, dept_scope=dept_scope,
                               campus_scope=campus_scope)

    group_col = "dept_code" if dimension == "dept" else dimension
    names = {}
    enum_map = dim.get("enum_map") or {}
    if isinstance(enum_map, dict):
        names = {str(k): str(v) for k, v in enum_map.items()}

    min_size = int(min_group_size if min_group_size is not None
                   else (registry.get("defaults") or {}).get("min_cell_size") or 5)
    rows = _to_group_rows(base_rows, cur_rows, metric, group_col, per_case, names)
    if not rows:
        raise AttributionError("两期都没有取到分组数据，无法分解")

    d = decompose(rows, min_group_size=min_size)
    text = render(d, metric_name=meta.get("name") or metric,
                  unit=meta.get("unit") or "", dim_name=dim.get("name") or dimension,
                  level_name=_level_name(meta))

    return {
        "applied": True,
        "metric": metric,
        "metric_name": meta.get("name"),
        "dimension": dimension,
        "dimension_name": dim.get("name"),
        "period": period,
        "periods": {
            "base": [spans["base"][0].isoformat(), (spans["base"][1] - dt.timedelta(days=1)).isoformat()],
            "current": [spans["current"][0].isoformat(), (spans["current"][1] - dt.timedelta(days=1)).isoformat()],
        },
        "value_semantics": "per_case" if per_case else "total",
        "group_count": len(rows),
        "decomposition": d.as_dict(),
        "narrative": text,
        "warnings": d.warnings,
        "sql": {"base": base_sql, "current": cur_sql},
    }


# ────────────────────────────────────────────────────────────
# 自测（不依赖数据库：用假执行器返回两期分组数据）
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    import re

    from app.core import engine_bridge

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    BASE = {"CARD": (10000.0, 10), "NEURO": (20000.0, 5)}
    CUR = {"CARD": (12000.0, 12), "NEURO": (21000.0, 5)}

    def fake_exec(sql: str):
        # 用 SQL 里的起始日期判断这是基期还是报告期（2026-08 / 2026-09）
        table = BASE if "2026-08-01" in sql else CUR
        return [{"dept_code": k, "ip_avg_fee": v, "_sample_size": c}
                for k, (v, c) in table.items()]

    class FakePipe:
        executor = staticmethod(fake_exec)

        def ask(self, q, role=None, dept_scope=(), campus_scope=None,
                resolved=None, inject_dsl=None, context=None):
            from dsl import dsl_from_dict
            from pipeline import AskPipeline
            p = AskPipeline(engine_bridge.load_registry(), {}, None, fake_exec,
                            dt.date(2026, 10, 15))
            return p.ask(q, role=role, dept_scope=dept_scope,
                         campus_scope=campus_scope, inject_dsl=inject_dsl)

    orig = engine_bridge.get_pipeline
    engine_bridge.get_pipeline = lambda force=False: FakePipe()
    try:
        out = run(metric="ip_avg_fee", dimension="dept", period="mom",
                  role="院长", dept_scope=["*"], today=dt.date(2026, 10, 15))
        d = out["decomposition"]
        # 基期例均 = (10000*10 + 20000*5) / 15；报告期 = (12000*12 + 21000*5) / 17
        check("基期例均由分组数据正确还原",
              abs(d["p0"] - 200000 / 15) < 1e-6, f"p0={d['p0']}")
        check("报告期例均由分组数据正确还原",
              abs(d["p1"] - 249000 / 17) < 1e-6, f"p1={d['p1']}")
        check("恒等式自检通过（结构+价格+交互+组别 = ΔP）", d["identity_ok"] is True)
        check("两期区间正确（base=2026-08, current=2026-09）",
              out["periods"]["base"][0] == "2026-08-01"
              and out["periods"]["current"][0] == "2026-09-01")
        check("解读文本非空且不含因果词",
              bool(out["narrative"]) and "导致" not in out["narrative"])

        # 人次类指标拒绝分解
        try:
            run(metric="ip_discharge_cnt", dimension="dept", role="院长",
                dept_scope=["*"], today=dt.date(2026, 10, 15))
            check("人次类指标被拒绝", False)
        except AttributionError as e:
            check("人次类指标被拒绝", "人次" in str(e), str(e)[:50])

        # 指标不支持的维度拒绝
        try:
            run(metric="ip_avg_fee", dimension="doctor", role="院长",
                dept_scope=["*"], today=dt.date(2026, 10, 15))
            check("指标不支持的维度被拒绝", False)
        except AttributionError as e:
            check("指标不支持的维度被拒绝", "不支持" in str(e), str(e)[:50])

        # 不存在的对比方式
        try:
            periods("qoq", dt.date(2026, 10, 15))
            check("非法对比方式被拒绝", False)
        except AttributionError:
            check("非法对比方式被拒绝", True)
    except Exception as e:  # noqa: BLE001
        check(f"归因自测异常: {e}", False)
    finally:
        engine_bridge.get_pipeline = orig
    return passed


if __name__ == "__main__":
    import sys
    ok = selftest()
    print("归因自测: " + ("全部通过" if ok else "存在失败"))
    sys.exit(0 if ok else 1)
