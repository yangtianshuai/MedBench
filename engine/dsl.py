"""查询 DSL —— 问数引擎与语义层之间的唯一契约。

设计原则（对应 docs/01 ADR）：
  1. LLM 只输出本 DSL，绝不输出 SQL。
  2. DSL 中只允许出现语义层已注册的代码，不允许出现任何表达式。
  3. 口径参数（date_basis / dept_basis / dedup）必须在 measure 的 supported 列表内，
     防止模型编造口径 —— 这是「受限生成」的核心防线。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Literal, Optional


Intent = Literal[
    "query_metric", "compare", "rank", "trend", "attribution",
    "meta_question", "need_clarify", "out_of_scope", "unsupported_metric",
]

# ★ 运行时白名单：Literal 只在静态检查生效，dataclass 不校验赋值。
#   不在校验层拦住，模型输出 intent="query" 会一路走到 SQL 生成（实测已发生）。
ALLOWED_INTENTS: tuple = Intent.__args__

ALLOWED_FILTER_OPS = ("=", "!=", "in", "not_in", ">=", "<=", "between")

# ★ 时间表达别名：模型说的是人话，内部枚举是另一套。
#   不做这层归一，「本月」被写成 this_month 就会直接触发「不支持的时间范围」而失败
#   —— 这是真实模型评测中出现过的失败（见 eval/reports）。
# ★ intent 别名：模型常用同义词（query / ranking / comparison...）。
#   实测 deepseek-chat 连续 3 轮把 intent 写成 "query"，回炉提示也纠不回来，
#   最终整条用例失败。翻译模型方言是语义层的职责，不该指望模型记住内部枚举。
INTENT_ALIASES = {
    "query": "query_metric", "query_data": "query_metric", "select": "query_metric",
    "metric_query": "query_metric", "aggregation": "query_metric", "查询": "query_metric",
    "comparison": "compare", "compare_metric": "compare", "对比": "compare",
    "ranking": "rank", "topn": "rank", "top_n": "rank", "排序": "rank",
    "trend_analysis": "trend", "trend_query": "trend", "趋势": "trend",
    "reason": "attribution", "why": "attribution", "归因": "attribution",
    "meta": "meta_question", "definition": "meta_question", "口径": "meta_question",
    "clarify": "need_clarify", "clarification": "need_clarify", "反问": "need_clarify",
    "unsupported": "out_of_scope", "out_of_range": "out_of_scope", "超范围": "out_of_scope",
}

# ★ compare 别名：模型会写 previous_period / 环比 等，内部只认 none/yoy/mom。
#   实测出现过 compare="previous_period" 一路通过校验（未被任何白名单拦住），
#   最终被静默当成「不对比」—— 用户拿到的是缺少对比列的答案却毫无提示。
COMPARE_ALIASES = {
    "previous_period": "mom", "prev_period": "mom", "last_period": "mom",
    "环比": "mom", "上月比": "mom", "mom": "mom",
    "同比": "yoy", "去年同期": "yoy", "同期": "yoy", "yoy": "yoy",
    "no": "none", "false": "none", "无": "none", "不对比": "none",
}

TIME_RANGE_ALIASES = {
    "this_month": "current_month", "current_month": "current_month",
    "this_year": "year_to_date", "current_year": "year_to_date",
    "ytd": "year_to_date", "year_to_now": "year_to_date",
    "last_year": "last_12_months", "last_1_month": "last_month",
    "this_week": "current_week", "last_week": "week_before_last",
}


def normalize_dsl_dict(data: Dict[str, Any],
                       registry: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """把「人话」的 DSL 归一成语义层内部表达（幂等）。

    两件事：
      1) 时间 type 别名 → 内部枚举
      2) filters.value 的中文枚举名 → code（如「骨科」→ ORTHO）
    ★ 原则：不能要求模型记住内部编码，翻译工作属于语义层。
    """
    if not isinstance(data, dict):
        return data

    if data.get("intent") in INTENT_ALIASES:
        data["intent"] = INTENT_ALIASES[data["intent"]]

    t = (data.get("time") or {})
    if isinstance(t, dict) and t.get("compare") in COMPARE_ALIASES:
        t["compare"] = COMPARE_ALIASES[t["compare"]]
    rng = t.get("range")
    if isinstance(t, dict) and isinstance(rng, dict):
        key = str(rng.get("type", "")).strip().lower()
        if key in TIME_RANGE_ALIASES:
            rng["type"] = TIME_RANGE_ALIASES[key]

    if registry:
        maps: Dict[str, Dict[str, str]] = {}
        for d in (registry.get("dimensions") or []):
            m = {str(k): v for k, v in (d.get("enum_map") or {}).items()}
            if m:
                maps[d.get("code")] = m
        for f in (data.get("filters") or []):
            if not isinstance(f, dict):
                continue
            m = maps.get(f.get("field"))
            if not m:
                continue
            v = f.get("value")
            if isinstance(v, list):
                f["value"] = [m.get(str(x), x) for x in v]
            elif str(v) in m:
                f["value"] = m[str(v)]
    return data

DateBasis = Literal["admit", "discharge", "settle", "visit", "operation"]
DeptBasis = Literal["order", "exec", "belong", "admit", "discharge"]
Dedup = Literal["visits", "patients"]
Granularity = Literal["day", "week", "month", "quarter", "year"]
CompareType = Literal["none", "yoy", "mom"]


class DSLError(ValueError):
    """DSL 校验失败。错误信息会回传给 LLM 做自我修正（最多 2 次）。"""


@dataclass
class TimeSpec:
    """时间范围。range 支持两种形式：
      - 相对：{"type": "last_month" | "last_n_months", "n": 6} 等
      - 绝对：{"type": "absolute", "start": "2025-01-01", "end": "2025-12-31"}
    ★ 绝对区间的 end 是**含当天**的闭区间上界（人类的写法）。
      转成 SQL 的半开区间 [start, end) 只发生在 sql_builder.resolve_time_range 一处。
      写成"下一天"（end="2026-01-01"）会**少算最后一天**，且答案看着正常。
    """
    granularity: Granularity = "month"
    range: Dict[str, Any] = field(default_factory=lambda: {"type": "last_month"})
    date_basis: Optional[DateBasis] = None      # None = 用指标默认口径
    compare: CompareType = "none"


@dataclass
class Filter:
    field: str
    op: Literal["=", "!=", "in", "not_in", ">=", "<=", "between"]
    value: Any


@dataclass
class Options:
    dept_basis: Optional[DeptBasis] = None      # None = 用指标默认口径
    dedup: Optional[Dedup] = None               # None = 用指标默认口径
    top_n: Optional[int] = None
    order_by: Optional[Dict[str, str]] = None   # {"metric": "ip_los_avg", "dir": "desc"}
    include_provisional: bool = True            # 是否包含未结算数据
    min_cell_size: int = 5                      # 小样本抑制阈值


@dataclass
class QueryDSL:
    intent: Intent
    metrics: List[str] = field(default_factory=list)
    dimensions: List[str] = field(default_factory=list)
    filters: List[Filter] = field(default_factory=list)
    time: TimeSpec = field(default_factory=TimeSpec)
    options: Options = field(default_factory=Options)
    confidence: float = 1.0

    # need_clarify / unsupported_metric 时才用
    ask: Optional[str] = None
    clarify_options: Optional[List[str]] = None
    suggest: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ────────────────────────────────────────────────────────────
# 校验
# ────────────────────────────────────────────────────────────
def validate_dsl(dsl: QueryDSL, registry: Dict[str, Any]) -> List[str]:
    """返回错误列表；空列表表示通过。

    registry: 解析后的 metric_registry.yaml（dict）
    """
    errs: List[str] = []

    # 0. intent 必须是受控枚举（Literal 不做运行时校验，必须显式拦）
    if dsl.intent not in ALLOWED_INTENTS:
        errs.append(f"intent '{dsl.intent}' 不在受控枚举内"
                    f"（可用：{', '.join(ALLOWED_INTENTS)}）")
        return errs

    metrics_idx = {m["code"]: m for m in registry.get("metrics", [])}
    measures_idx = {m["code"]: m for m in registry.get("measures", [])}
    dims_idx = {d["code"]: d for d in registry.get("dimensions", [])}

    # 1. 无需查数的意图直接放行
    if dsl.intent in ("need_clarify", "out_of_scope", "unsupported_metric",
                      "meta_question"):
        return errs

    # 2. 指标存在性
    if not dsl.metrics:
        errs.append("metrics 不能为空")
    for code in dsl.metrics:
        if code not in metrics_idx:
            errs.append(f"指标 '{code}' 未在语义层注册"
                        f"（可用：{', '.join(sorted(metrics_idx))[:200]}）")
            continue

        metric = metrics_idx[code]

        # 3. 维度是否被该指标支持
        supported_dims = metric.get("dimensions", []) or []
        for d in dsl.dimensions:
            if d not in dims_idx:
                errs.append(f"维度 '{d}' 未在语义层注册")
            elif supported_dims and d not in supported_dims:
                errs.append(f"指标 '{code}' 不支持维度 '{d}'"
                            f"（支持：{', '.join(supported_dims)}）")

        # 4. 口径参数必须在 supported 内 —— 防止模型编造口径
        _check_basis(errs, code, metric, measures_idx, dsl)

    # 4.5 过滤算子白名单
    #     ★ 必须有这一层：Filter 是 dataclass，Filter(op="LIKE") 不会报错，
    #       若不在校验层拦住，会一路拼进 SQL（等价于把表达式控制权交给了模型）。
    for f in dsl.filters:
        if f.op not in ALLOWED_FILTER_OPS:
            errs.append(f"不支持的过滤算子 '{f.op}'（可用：{', '.join(ALLOWED_FILTER_OPS)}）")
        elif f.op in ("in", "not_in") and not isinstance(f.value, (list, tuple)):
            errs.append(f"算子 '{f.op}' 的 value 必须是数组")
        elif f.op == "between" and not (isinstance(f.value, (list, tuple))
                                        and len(f.value) == 2):
            errs.append("算子 'between' 的 value 必须是 [start, end]（end 为开区间）")

    # 4.6 对比方式白名单（翻译之后仍不合法 → 明确报错，绝不静默降级）
    if dsl.time.compare not in ("none", "yoy", "mom"):
        errs.append(f"不支持的对比方式 '{dsl.time.compare}'（可用：none, yoy, mom）")

    # 5. 时间范围合理性
    r = dsl.time.range or {}
    if r.get("type") == "absolute":
        if not r.get("start") or not r.get("end"):
            errs.append("绝对时间范围必须同时提供 start 与 end（end 为含当天的闭区间）")
        # ★ start == end 合法：闭区间下它就是"单日"（如「12月31日」→ [12-31, 01-01)）。
        #   此前写的是 `>=`，那是半开区间的判断（start == end 等于空区间）；
        #   契约改成闭区间后，再用 >= 会把单日查询判成非法。
        elif str(r["start"]) > str(r["end"]):
            errs.append("时间范围 start 不能晚于 end")
    max_span = (registry.get("defaults", {}) or {}).get("max_time_span_months", 60)
    if r.get("type") == "last_n_months" and int(r.get("n", 0)) > max_span:
        errs.append(f"时间跨度超过上限 {max_span} 个月")

    # 6. 阈值与上限
    defaults = registry.get("defaults", {}) or {}
    max_rows = defaults.get("max_result_rows", 100000)
    if dsl.options.top_n is not None and dsl.options.top_n > max_rows:
        errs.append(f"top_n 超过上限 {max_rows}")
    if dsl.options.min_cell_size < defaults.get("min_cell_size", 5):
        errs.append("min_cell_size 不得小于系统下限 5（小样本抑制不可关闭）")

    return errs


def _check_basis(errs, code, metric, measures_idx, dsl):
    """校验 date_basis / dept_basis / dedup 是否在支持的口径内。"""
    if metric.get("type") == "ratio":
        # ratio 指标的口径由其分子 measure 决定
        num = metric.get("numerator")
        ref = measures_idx.get(num) if isinstance(num, str) else None
    else:
        ref = measures_idx.get(metric.get("measure"))
    if ref is None:
        return

    db = dsl.time.date_basis
    if db:
        supported = (ref.get("date_basis", {}) or {}).get("supported", [])
        if supported and db not in supported:
            errs.append(f"指标 '{code}' 不支持时间口径 '{db}'（支持：{', '.join(supported)}）")

    ob = dsl.options.dept_basis
    if ob:
        supported = (ref.get("dept_basis", {}) or {}).get("supported", [])
        if supported and ob not in supported:
            errs.append(f"指标 '{code}' 不支持科室口径 '{ob}'（支持：{', '.join(supported)}）")

    dd = dsl.options.dedup
    if dd:
        allowed = [ref.get("dedup"), "visits", "patients"]
        if dd not in allowed:
            errs.append(f"指标 '{code}' 不支持去重方式 '{dd}'")


def dsl_from_dict(data: Dict[str, Any],
                  registry: Optional[Dict[str, Any]] = None) -> QueryDSL:
    """从 LLM 输出的 JSON 构造 DSL（容错：忽略未知字段）。

    传 registry 时会先做「人话 → 内部枚举」归一化（时间别名、中文枚举值转 code）。
    ★ 所有调用点都应传 registry，否则归一化形同虚设。
    """
    data = normalize_dsl_dict(data, registry)
    t = data.get("time") or {}
    o = data.get("options") or {}
    return QueryDSL(
        intent=data.get("intent", "query_metric"),
        metrics=list(data.get("metrics") or []),
        dimensions=list(data.get("dimensions") or []),
        filters=[Filter(**f) for f in (data.get("filters") or [])],
        time=TimeSpec(
            granularity=t.get("granularity", "month"),
            range=t.get("range") or {"type": "last_month"},
            date_basis=t.get("date_basis"),
            compare=t.get("compare", "none"),
        ),
        options=Options(
            dept_basis=o.get("dept_basis"),
            dedup=o.get("dedup"),
            top_n=o.get("top_n"),
            order_by=o.get("order_by"),
            include_provisional=o.get("include_provisional", True),
            min_cell_size=o.get("min_cell_size", 5),
        ),
        confidence=float(data.get("confidence", 1.0)),
        ask=data.get("ask"),
        clarify_options=data.get("clarify_options"),
        suggest=data.get("suggest"),
    )
