#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端问数管线 —— 把 dsl / sql_builder / semantic-layer 串成一条可跑的链路。

    question → 指标召回 → 歧义反问 → LLM 生成 DSL → DSL 校验(可回炉2次)
             → 权限门禁 → 生成 SQL → 执行 → 结果校验 → 口径回显

★ 本模块要回答的核心问题（此前从未验证）：
  「LLM 输出不合法 DSL 时，校验层到底能不能拦住？」

  验证方法不是靠真 LLM 碰运气，而是用两个可控的假 LLM 做对拍：
    MockOracleLLM   —— 永远输出标注用例里的正确 DSL
    PerturbedLLM    —— 在正确 DSL 上注入 LLM 的典型错误
  前者跑通说明链路无阻塞，后者全被拦截才说明防线有效。

用法:
    python pipeline.py --selftest
    python pipeline.py --ask "上个月全院出院人次是多少" --role 院长
    python pipeline.py --ask "各科室平均住院日排名前10" --role 医务处 --verbose
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")

sys.path.insert(0, _HERE)

from dsl import (ALLOWED_INTENTS, DSLError, Filter, Options, QueryDSL,  # noqa: E402
                 TimeSpec, dsl_from_dict, normalize_dsl_dict, validate_dsl,
                 CompareType, DateBasis, Dedup, DeptBasis, Granularity)
from fewshot import ExampleIndex, load_examples  # noqa: E402  （Few-shot 示例选择）
from recall import RecallIndex  # noqa: E402  （指标召回：归一化/碎片/主题域三路打分）
from recall import selftest as _recall_selftest  # noqa: E402
from sql_builder import SQLBuilder  # noqa: E402


class PipelineError(RuntimeError):
    """管线不可恢复错误。"""


# ────────────────────────────────────────────────────────────
# 结果
# ────────────────────────────────────────────────────────────
@dataclass
class AskResult:
    status: str                       # answered / need_clarify / denied / out_of_scope / error / meta
    question: str
    dsl: Optional[QueryDSL] = None
    raw_dsl: Optional[Dict[str, Any]] = None   # ★ LLM 原始输出（审计留痕 / 评估用）
    sql: Optional[str] = None
    caliber: Optional[Dict[str, Any]] = None
    rows: Optional[List[Dict[str, Any]]] = None
    answer: str = ""
    warnings: List[str] = field(default_factory=list)
    trace: List[Dict[str, Any]] = field(default_factory=list)
    # ★ 结构化反问项（F1.3 卡片式反问的数据契约，docs/13 §7 C1）：
    #   [{id, name, ambiguity, ask, options, default, remember}]
    #   此前只有 answer 里的一段文字，前端无法做卡片，只能靠正则抠。
    clarify: List[Dict[str, Any]] = field(default_factory=list)

    def step(self, name: str, detail: Any = None):
        self.trace.append({"step": name, "detail": detail})

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status, "question": self.question,
            "dsl": self.raw_dsl,
            "sql": self.sql, "caliber": self.caliber,
            "rows": self.rows, "answer": self.answer,
            "warnings": self.warnings, "trace": self.trace,
            "clarify": self.clarify,
        }


def clarify_item(rule: Dict[str, Any]) -> Dict[str, Any]:
    """歧义规则 → 前端可渲染的反问项（只做字段搬运，不解释、不推导口径）。

    options_detail 额外标出**每个选项能否被自动应用**（来自语义层的 apply 声明）：
    不能应用的选项若被选中，管线会拒绝出数并说明原因，界面应提前标注，
    别让用户选一个注定得不到答案的选项（docs/13 §7 C9）。
    """
    ap = rule.get("apply") or {}
    declared = set((ap.get("values") or {}).keys()) | set((ap.get("set") or {}).keys())
    aliases = set(ap.get("alias_to_default") or [])
    blocked = ap.get("unresolvable_values") or {}
    detail: List[Dict[str, Any]] = []
    for o in (rule.get("options") or []):
        if o in declared:
            detail.append({"label": o, "appliable": True, "reason": None})
        elif o in aliases:
            detail.append({"label": o, "appliable": True,
                           "reason": "本院默认口径（选了按默认口径出数，数值与口径回显一致）"})
        elif o in blocked:
            detail.append({"label": o, "appliable": False, "reason": blocked[o]})
        else:
            detail.append({"label": o, "appliable": False,
                           "reason": ap.get("unresolvable")
                           or "语义层未声明该选项如何映射到指标/字段"})
    return {
        "id": rule.get("id"),
        "name": rule.get("name"),
        "ambiguity": rule.get("ambiguity"),
        "ask": rule.get("ask") or rule.get("ambiguity") or "",
        "options": list(rule.get("options") or []),
        "options_detail": detail,
        "default": rule.get("default"),
        "remember": bool(rule.get("remember_user_choice")),
        "note": rule.get("note"),
    }


def _index(items: Any, key: str = "code") -> Dict[str, Any]:
    """把语义层中的 list-of-dict 建成 code→meta 索引。

    ★ 只读，不修改传入的 registry —— 曾因就地转成 dict 破坏 sql_builder 的假设。
      若已是 dict 则原样返回（兼容两种形态）。
    """
    if isinstance(items, dict):
        return items
    return {i[key]: i for i in (items or [])}


# ────────────────────────────────────────────────────────────
# 指标召回
# ────────────────────────────────────────────────────────────
_recall_cache: Dict[int, Any] = {}


def recall_metrics(question: str, registry: Dict[str, Any], top_k: int = 8) -> List[Dict[str, Any]]:
    """关键词 + 别名 + 主题域三路混合召回（实现在 engine/recall.py）。

    ★ 召回只做「候选 narrowing」，绝不做「指标认定」——认定权在 LLM，否决权在语义层。

    历史问题（2026-10 修复，见 eval/eval_recall.py 基线报告）：
      ① 纯子串匹配 → "门诊多少人次" 对不上别名「门诊人次」，返回空候选；
      ② 长度惩罚 10-len → 长别名（31日内非计划再入院率）几乎拿不到分；
      ③ 空候选不兜底 → 模型拿不到任何指标，只能凭空编造 code。
    """
    idx = _recall_cache.get(id(registry))
    if idx is None or getattr(idx, "registry", None) is not registry:
        # 语义层热更新会换掉 registry 对象；只保留最近几个版本，避免缓存无界增长
        if len(_recall_cache) >= 8:
            _recall_cache.clear()
        idx = RecallIndex(registry)
        _recall_cache[id(registry)] = idx
    return idx.search(question, top_k)


# ────────────────────────────────────────────────────────────
# 歧义反问
# ────────────────────────────────────────────────────────────
# 只有这些意图才需要反问口径；问定义/超范围的反问属于答非所问
ASKABLE_INTENTS = {"query_metric", "compare", "rank", "trend", "attribution"}


def _measure_of(registry: Dict[str, Any], metric_code: Optional[str]) -> Optional[Dict[str, Any]]:
    """取指标对应的 measure（ratio 取分子），用于查口径 supported 列表。"""
    if not metric_code:
        return None
    metrics = _index(registry.get("metrics"))
    measures = _index(registry.get("measures"))
    m = metrics.get(metric_code)
    if not m:
        return None
    if m.get("type") == "ratio":
        num = m.get("numerator")
        code = num[0] if isinstance(num, list) else num
        return measures.get(code)
    return measures.get(m.get("measure"))


def _supported_len(ref: Optional[Dict[str, Any]], kind: str) -> int:
    if not ref:
        return 0
    return len(((ref.get(kind) or {}).get("supported")) or [])


def _dept_mentioned(question: str, registry: Dict[str, Any]) -> bool:
    """问题中是否点名了具体科室（用于科室归属歧义的触发）。

    ★ 用 registry 的科室枚举判定，而不是裸词「科室」：
      「各科室排名」的「各科室」是分组维度，点名科室才是归属歧义。
    """
    dim = _index(registry.get("dimensions")).get("dept") or {}
    return any(str(name) in question for name in (dim.get("enum") or []))


def _rule_triggered(rule: Dict[str, Any], question: str,
                    dsl: Optional[QueryDSL], registry: Dict[str, Any]) -> bool:
    """反问的五个必要条件（N1~N5），全部满足才反问。"""
    # R3：只回显、不反问的口径
    if rule.get("ask_mode", "ask") != "ask":
        return False

    # N2：问题中已自带消歧标记
    if any(str(m) in question for m in (rule.get("explicit_markers") or [])):
        return False

    # N1：命中触发条件
    if rule.get("trigger_on") == "dept_mentioned":
        if not _dept_mentioned(question, registry):
            return False
    elif not any(str(t) in question for t in (rule.get("triggers") or [])):
        return False

    if dsl is None:
        return True

    # N3：该口径必须真的有多个可选值，否则问了也只有一个答案
    ref = _measure_of(registry, dsl.metrics[0] if dsl.metrics else None)
    for kind, n in (rule.get("supported_min") or {}).items():
        if _supported_len(ref, kind) < int(n):
            return False

    # N5：DSL 侧已消歧
    dc = rule.get("dsl_check") or {}
    if dc.get("compare") == "not_none" and (dsl.time.compare or "none") != "none":
        return False

    return True


# ────────────────────────────────────────────────────────────
# 口径写回：把用户澄清的选项落到 DSL 上（docs/13 §7 C9）
# ────────────────────────────────────────────────────────────
# ★ 映射不写在这里，写在语义层 ambiguity_rules.yaml 的 apply 段。
#   代码只认识下面这些 DSL 路径，不认识任何业务口径 ——
#   否则「结算日期=settle」这种院方知识就跑进代码里了（同 ADR-03 的理由）。
APPLY_PATHS = (
    "metrics",
    "time.date_basis", "time.compare", "time.granularity", "time.range",
    "options.dept_basis", "options.dedup", "options.top_n", "options.order_by",
)


def _set_path(obj: Any, path: str, value: Any) -> None:
    parts = path.split(".")
    cur = obj
    for p in parts[:-1]:
        cur = getattr(cur, p)
    setattr(cur, parts[-1], value)


def _subst(value: Any, dsl: QueryDSL) -> Any:
    """占位符替换：@metric → 当前指标码（只用于 order_by.metric 这类必须自指的字段）。"""
    if isinstance(value, str):
        return dsl.metrics[0] if (value == "@metric" and dsl.metrics) else value
    if isinstance(value, dict):
        return {k: _subst(v, dsl) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, dsl) for v in value]
    return value


def _apply_assignments(ap: Dict[str, Any], choice: str,
                       dsl: QueryDSL) -> Optional[Dict[str, Any]]:
    """选项 → {DSL 路径: 取值}；语义层没声明这个选项时返回 None（不许猜）。"""
    st = ap.get("set") or {}
    if st:
        got = st.get(choice)
        return dict(got) if isinstance(got, dict) else None
    field = ap.get("field")
    values = ap.get("values") or {}
    if not field or choice not in values:
        return None
    value = values[choice]
    if field == "time.range":
        if isinstance(value, dict):
            return {field: value}
        rt = ap.get("range_type") or "last_n_months"
        return {field: ({"type": rt, "n": value} if rt == "last_n_months" else {"type": rt})}
    return {field: value}


def apply_resolved(dsl: QueryDSL, resolved: Optional[Dict[str, str]],
                   rules: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """把用户澄清的口径写回 DSL（就地修改传入的 dsl，调用方负责先做副本）。

    返回 (applied, unapplied)：
        applied   —— [{"id","name","choice","field","value"}]
        unapplied —— [{"id","name","choice","options","reason"}]

    ★ 没声明映射的选项**不会被静默忽略**：返回给调用方去拒绝出数并说明原因。
      静默忽略的下场就是 C9 —— 用户选了「结算日期」，系统按默认口径出数，
      界面还显示"已按所选口径重算"。
    ★ 两种「无需改动」：① 选项值就是该规则的 default；② 选项被声明在
      apply.alias_to_default（院方认可的同义说法，如「住院期间死亡」= 院内死亡）。
      默认值本身声明在 metric_registry 的 date_basis.default 等字段上，不在这里维护第二份。
    """
    resolved = resolved or {}
    if not resolved:
        return [], []
    by_id = {r.get("id"): r for r in (rules.get("rules") or [])}
    applied: List[Dict[str, Any]] = []
    unapplied: List[Dict[str, Any]] = []

    for rid, raw_choice in resolved.items():
        choice = "" if raw_choice is None else str(raw_choice)
        rule = by_id.get(rid) or {}
        item = {"id": rid, "name": rule.get("name"), "choice": choice,
                "options": list(rule.get("options") or [])}
        if not rule:
            unapplied.append({**item, "reason": f"歧义词表里没有规则 '{rid}'，无法确认这条口径的含义"})
            continue

        ap = rule.get("apply") or {}
        if choice == str(rule.get("default") or ""):
            continue                                  # 选项就是该规则的默认值 = 无需改动
        if choice in (ap.get("alias_to_default") or []):
            continue                                  # 院方声明「这个说法 = 本院默认口径」
        if not ap:
            unapplied.append({**item, "reason": (
                f"语义层还没声明规则 '{rid}' 的口径如何映射到指标/字段，"
                f"无法据此出数（选了就当没选是更坏的结果）")})
            continue
        if ap.get("unresolvable"):
            unapplied.append({**item, "reason": ap["unresolvable"]})
            continue

        assigns = _apply_assignments(ap, choice, dsl)
        if assigns is None:
            reason = ((ap.get("unresolvable_values") or {}).get(choice)
                      or f"语义层未声明选项「{choice}」如何映射到指标/字段")
            unapplied.append({**item, "reason": reason})
            continue

        bad_path = next((p for p in assigns if p not in APPLY_PATHS), None)
        if bad_path:
            unapplied.append({**item, "reason": (
                f"语义层声明的 DSL 路径 '{bad_path}' 不合法"
                f"（可用：{', '.join(APPLY_PATHS)}）")})
            continue

        for path, value in assigns.items():
            _set_path(dsl, path, _subst(value, dsl))
        applied.append({**item,
                        "field": "、".join(assigns.keys()),
                        "value": "、".join(_short(v) for v in assigns.values())})
    return applied, unapplied


def _short(v: Any) -> str:
    s = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def detect_ambiguity(question: str, rules: Dict[str, Any],
                     dsl: Optional[QueryDSL] = None,
                     registry: Optional[Dict[str, Any]] = None,
                     resolved: Optional[Dict[str, str]] = None,
                     max_asks: int = 2) -> List[Dict[str, Any]]:
    """命中歧义词表且上下文中无消歧信息 → 返回待反问项。

    resolved: 用户已澄清的内容，如 {"AMB-001": "有效就诊人次"}
    """
    resolved = resolved or {}
    registry = registry or {}
    hits = []
    for r in (rules.get("rules") or []):
        rid = r.get("id")
        if rid in resolved:                                    # N4：已澄清
            continue
        if not _rule_triggered(r, question, dsl, registry):
            continue
        hits.append(r)
        if len(hits) >= max_asks:
            break
    return hits


# ────────────────────────────────────────────────────────────
# 提示词
# ────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是医院数据查询助手。你的唯一职责：把用户的自然语言问题翻译成【查询 DSL】。

硬性约束（违反即为错误输出）：
1. 只能输出 JSON 对象，不得输出 SQL、不得输出解释性文字、不得输出 markdown 代码块。
2. metrics / dimensions / filters.field 只能使用下面「候选」中给出的 code，禁止编造。
3. date_basis / dept_basis / dedup 只能取该指标 supported 列表中的值；不确定就省略，由系统填默认值。
4. 不得自行推导任何医疗公式（平均住院日 = 床日数/出院人次 等），这些已固化在语义层。
5. ★ 只在【信息真的缺失】时才输出 intent="need_clarify"（例如没给时间、指标无法区分）。
   口径存在本院默认值时**不要反问**，直接按默认出数：系统会在结果上回显口径供用户一键切换。
   （"门诊量""收入""手术量"等虽有歧义，但本院已有默认口径，反问属于打扰，不是严谨。）
6. 用户问「指标怎么算的」时，输出 intent="meta_question"，不要查数。
7. 问题超出数据范围时，输出 intent="out_of_scope"，不要编造。
   （候选指标里没有该指标 → out_of_scope。**不要用 need_clarify 去追问范围**；
     ★ 反之，只要候选里有对应指标，即使问题表达模糊（如「收入怎么样」），
       也必须出数 —— 口径不清时用默认口径（见第 5 条），out_of_scope 只用于「真的没有」。）
8. filters.value 优先写「候选维度枚举」里的 code（如科室写 ORTHO）；写中文名也可以，系统会自动翻译。
9. 时间优先写相对类型（last_month / current_month / last_n_months / year_to_date /
   last_quarter / same_period_last_year / week_before_last / last_3_years 等）。
9.1 ★ 绝对区间 {"type":"absolute"} 的 **end 是"含当天"的闭区间上界**（写成区间真实
   结束的那一天，不要写成它的下一天）：
     「2023年」    → {"type":"absolute","start":"2023-01-01","end":"2023-12-31"}
     「2025年7月」 → {"type":"absolute","start":"2025-07-01","end":"2025-07-31"}
     「2025年三季度」→ {"type":"absolute","start":"2025-07-01","end":"2025-09-30"}
   ✗ 禁止写 end="2024-01-01" 这类"下一天"：那会让整段区间**少算最后一天**，
     而答案看起来完全正常（这是最危险的错法，不会被任何校验拦住）。
10. ★ 量词必须与指标的 unit 一致：问题说「人次」就选 unit=人次 的指标（如 op_visit_cnt），
    说「人数/多少患者」才选 *_patient_cnt（去重人数）。二者的数值不同，选错即为错误答案。

【口语表达约定】（本院管理口吻，按约定翻译，不要按字面理解）：
11. ★「……怎么样 / 如何 / 好不好 / 什么情况」= 要看**环比变化**：compare 写 "mom"
    （例：「上个月住院收入怎么样」= 上月收入 + 与再上一月对比，compare="mom"）；
    该约定对 trend / attribution 意图**同样适用**；
    「同比 / 比去年同期」→ compare="yoy"；明确说了「环比 / 比上月」→ compare="mom"。
    ★ 但 intent **不要**因此写成 "compare"：「……怎么样」仍是查询（query_metric / trend /
    attribution），只有明说「A 和 B 对比 / 各科室对比 / 排名前N」才用 "compare" / "rank"。
12. ★ 术语是**上位概念**时要展开，不要只取一种：
    「医保患者 / 医保病人」→ filters: {"field":"pay_type","op":"!=","value":"自费"}
    （医保 = 职工医保 + 居民医保，只写某一种会把数据算少）
    「自费患者」→ {"field":"pay_type","op":"=","value":"自费"}。
    ★「急诊 / 门诊 / 住院」是**就诊类型**而非科室：「急诊人次」→
    {"field":"visit_type","op":"=","value":"急诊"}，不要写成 dept=EM（那是急诊科这个科室）。
13. ★ 时间表达锚点（这几类最容易译错）：
    「前三季度」→ first_three_quarters（1月1日~9月30日），**不是** year_to_date；
    「今年以来 / 年初至今」→ year_to_date；
    「去年同期」→ same_period_last_year；
    「2025年三季度 / 2025年7月」这类带**具体年份**的 → 绝对区间（闭区间，见 9.1），
    不要写成 last_quarter。
14. ★ 排名/对比题的 dimensions = 被排名的对象：
    「各院区 / 各院 / 分院」→ campus；「各科室 / 各科 / 哪些科室」→ dept；
    「医生 / 医师 / 主刀」→ doctor（注意与"科室排名"区分）。

输出 JSON schema:
{"intent": "...", "metrics": [...], "dimensions": [...],
 "filters": [{"field":"...","op":"=","value":...}],
 "time": {"granularity":"month","range":{"type":"last_month"},"compare":"none"},
 "options": {"top_n":10,"order_by":{"metric":"...","dir":"desc"}},
 "confidence": 0.9}
"""


# ────────────────────────────────────────────────────────────
# 意图规则预判（路线 C 的模板化兜底）
# ────────────────────────────────────────────────────────────
# ★ 实测：LLM 能把指标选对、DSL 可执行，但意图细分（rank/trend/attribution）
#   识别很差 —— 「各科室排名前10」被判成 query_metric，结果丢了 ORDER BY/LIMIT，
#   给出的是「全部科室」而不是「前 10」。这类错误对用户是实质性的。
#   规则预判只作为提示注入，最终认定权仍在 LLM、否决权仍在语义层。
INTENT_HINT_RULES: List[Tuple[str, List[str]]] = [
    ("rank", ["排名", "排第几", "排在前", "最高", "最低", "前3", "前5", "前10",
              "前20", "前三", "top", "Top", "TOP"]),
    ("trend", ["趋势", "走势", "近半年", "近三年", "最近6个月", "逐月", "变化"]),
    ("attribution", ["为什么", "为啥", "涨了", "下降", "原因", "归因", "是什么导致"]),
    ("compare", ["对比", "相比", "哪个高", "谁更高", "对比一下"]),
]


def predict_intent(question: str) -> Optional[str]:
    """按强特征词预判意图（用于给 LLM 一个先验，不替代 LLM 判断）。"""
    for intent, words in INTENT_HINT_RULES:
        if any(w in question for w in words):
            return intent
    return None


# 维度强特征词：只认「多实体」表达（各院区/各科室/哪些科室…），
# 单个科室名（"神经外科收入多少"）是过滤条件，不是分组维度，不能在这里命中。
DIM_HINT_RULES = (
    ("campus", ["各院区", "各院", "分院", "院区之间", "哪个院区"]),
    ("dept", ["各科室", "各科", "哪些科室", "各临床科室", "科室之间", "哪个科室"]),
    ("doctor", ["各医生", "医生排名", "医师排名", "主刀", "哪些医生", "医生之间"]),
)


def predict_dimensions(question: str) -> List[str]:
    """按强特征词预判分组维度（给 LLM 先验，不替代 LLM 判断）。"""
    return [d for d, words in DIM_HINT_RULES if any(w in question for w in words)]


def _norm_context(context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """规整"多轮上文"（F1.2 指代消解的数据来源）。

    只接受 {question, dsl}，且 **dsl 必须非空**：
    没有可继承的口径时一律当"没有上文"，
    宁可让模型从头解析，也不要传一段空上下文让它误以为可以指代。
    """
    if not isinstance(context, dict):
        return None
    dsl = context.get("dsl")
    if not isinstance(dsl, dict) or not dsl:
        return None
    return {"question": str(context.get("question") or ""), "dsl": dsl,
            "metric_code": context.get("metric_code"), "status": context.get("status")}


def build_prompt(question: str, candidates: Sequence[Dict[str, Any]],
                 role: str, dept_scope: Sequence[str],
                 dimensions: Optional[Sequence[Dict[str, Any]]] = None,
                 intent_hint: bool = True,
                 context: Optional[Dict[str, Any]] = None,
                 examples: Optional[str] = None) -> str:
    cand = json.dumps(candidates, ensure_ascii=False, indent=2)
    # ★ 必须把维度的可取枚举告诉模型，否则它只能编造过滤值（如写「骨科」而不是 ORTHO）
    dims = ""
    if dimensions:
        brief = [{"code": d.get("code"), "name": d.get("name"),
                  "enum": (d.get("enum") or [])[:20],
                  "enum_map": d.get("enum_map") or None}
                 for d in dimensions if d.get("enum")]
        if brief:
            dims = ("\n【可过滤维度及取值】\n"
                    + json.dumps(brief, ensure_ascii=False, indent=2)
                    + "\n（filters.value 请优先写 enum_map 中的 code）\n")
    hint = ""
    guess = predict_intent(question) if intent_hint else None
    if guess:
        hint = (f"\n【意图预判】规则命中，倾向 intent=\"{guess}\"。"
                "若与你的判断不一致，以你的判断为准。\n")
    if intent_hint:
        gdims = predict_dimensions(question)
        if gdims:
            hint += (f"\n【维度预判】问题提到「{'/'.join(gdims)}」类多实体表达，"
                     f"dimensions 应至少包含 {gdims}。\n")
    ctx_block = ""
    if context:
        ctx_block = (
            "\n【对话上文】这是同一会话内的追问，上一轮实际执行的口径如下：\n"
            f"- 上一轮问题：{context.get('question') or '（未记录）'}\n"
            "- 上一轮实际执行的 DSL："
            + json.dumps(context.get("dsl"), ensure_ascii=False)
            + "\n规则：本轮若在做指代或省略（如「那心内科呢」「按季度看」「排除急诊」"
              "「再看一下上季度」），必须在上面的 DSL 基础上**只改被问到的部分**，"
              "其余字段（指标、时间口径、科室口径、去重口径、过滤）原样保留；"
              "若本轮其实问的是另一件事，则以本轮问题为准，不要硬套上文口径。\n")
    return (SYSTEM_PROMPT
            + f"\n\n【当前用户】角色={role}，数据范围={list(dept_scope)}\n"
            + f"【候选指标/维度】\n{cand}\n"
            + dims
            # ★ 示例放在候选与问题之间：既有"用法"示范，又不至于把问题挤到最后
            + (examples or "")
            + ctx_block
            + f"\n【用户问题】{question}\n"
            + hint
            + "\n【输出】只输出 JSON：")


# ────────────────────────────────────────────────────────────
# LLM 适配器（可插拔）
# ────────────────────────────────────────────────────────────
class LLMAdapter:
    """所有适配器返回【已解析的 dict】；解析失败由调用方按 error 处理。"""

    def generate(self, prompt: str, hint: Optional[List[str]] = None) -> Dict[str, Any]:
        raise NotImplementedError


class MockOracleLLM(LLMAdapter):
    """完美 LLM：始终输出标注用例中的正确 DSL。用于验证链路无阻塞。"""

    def __init__(self, dsl_dict: Dict[str, Any]):
        self.dsl_dict = dsl_dict

    def generate(self, prompt: str, hint: Optional[List[str]] = None) -> Dict[str, Any]:
        # 完美 LLM 会在收到校验错误后自我修正（这里直接返回正确版本）
        return json.loads(json.dumps(self.dsl_dict))


# LLM 的典型错误字典：id → 注入函数
FAULT_INJECTORS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {}


def fault(name: str):
    def deco(fn):
        FAULT_INJECTORS[name] = fn
        return fn
    return deco


@fault("hallucinated_metric")
def _f_metric(d):
    d["metrics"] = ["hospital_profit_margin"]        # 语义层未注册
    return d


@fault("bad_date_basis")
def _f_basis(d):
    d.setdefault("time", {})["date_basis"] = "operation"   # 出院指标不支持手术日口径
    return d


@fault("missing_time")
def _f_time(d):
    d.pop("time", None)
    return d


@fault("free_sql_injection")
def _f_sql(d):
    d["sql"] = "SELECT * FROM dwd_fact_inpatient_discharge WHERE 1=1"  # 试图绕过语义层
    return d


@fault("over_permission_dimension")
def _f_dim(d):
    d.setdefault("dimensions", [])
    if "doctor" not in d["dimensions"]:
        d["dimensions"].append("doctor")            # restricted 维度，需授权
    return d


@fault("unknown_dimension")
def _f_unk(d):
    d.setdefault("dimensions", []).append("patient_name")  # 未注册维度
    return d


@fault("bad_filter_op")
def _f_op(d):
    d.setdefault("filters", []).append(
        {"field": "dept", "op": "LIKE", "value": "CARD%"})   # 非法操作符
    return d


class PerturbedLLM(LLMAdapter):
    """会犯错的 LLM：在正确 DSL 上注入指定错误。用于验证防线是否真的拦得住。"""

    def __init__(self, dsl_dict: Dict[str, Any], faults: Sequence[str],
                 repair_on_hint: bool = False):
        self.base = json.loads(json.dumps(dsl_dict))
        self.faults = list(faults)
        self.repair_on_hint = repair_on_hint   # True = 收到校验错误后能自我修正

    def generate(self, prompt: str, hint: Optional[List[str]] = None) -> Dict[str, Any]:
        if hint and self.repair_on_hint:
            return json.loads(json.dumps(self.base))     # 回炉后修正
        d = json.loads(json.dumps(self.base))
        for f in self.faults:
            if f in FAULT_INJECTORS:
                d = FAULT_INJECTORS[f](d)
            else:
                raise PipelineError(f"未知 fault: {f}")
        return d


class HTTPLLM(LLMAdapter):
    """真实 LLM（OpenAI 兼容 /chat/completions）。未配置环境变量时报明确错误。"""

    def __init__(self, model: Optional[str] = None,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 timeout: int = 60):
        self.model = model or os.getenv("ASK_LLM_MODEL", "qwen2.5-72b-instruct")
        self.base_url = (base_url or os.getenv("ASK_LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.getenv("ASK_LLM_API_KEY", "")
        self.timeout = timeout
        if not self.base_url or not self.api_key:
            raise PipelineError(
                "未配置 ASK_LLM_BASE_URL / ASK_LLM_API_KEY。"
                "医院场景须指向院内网私有化部署的模型服务。")

    def generate_raw(self, prompt: str, system: Optional[str] = None) -> str:
        """返回模型原始文本（G0 裸 Text-to-SQL 基线用，不做 JSON 解析）。"""
        body = {"model": self.model,
                "messages": [{"role": "system", "content": system or ""},
                              {"role": "user", "content": prompt}],
                "temperature": 0}
        return self._post_text(body)

    def generate(self, prompt: str, hint: Optional[List[str]] = None) -> Dict[str, Any]:
        import urllib.error
        import urllib.request

        messages = [{"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt}]
        if hint:
            messages.append({"role": "user",
                             "content": "上次输出未通过校验：" + "; ".join(hint)
                                        + "。请修正后只输出 JSON。"})
        body = {"model": self.model, "messages": messages, "temperature": 0,
                "response_format": {"type": "json_object"}}
        try:
            return self._post(body)
        except urllib.error.HTTPError as e:
            # 部分厂商/版本不支持 response_format：降级为「只要求输出 JSON」后再试一次
            if e.code == 400 and "response_format" in body:
                body.pop("response_format")
                return self._post(body)
            raise

    def _post(self, body: dict) -> Dict[str, Any]:
        import urllib.request

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        return _parse_llm_json(content)

    def _post_text(self, body: dict) -> str:
        """只取原始文本（不解析 JSON），供裸 SQL 基线使用。"""
        import urllib.request

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]


def build_schema_digest(registry: Dict[str, Any]) -> str:
    """把语义层退化为「裸表结构」——G0 基线用。

    ★ 这不是给模型用的语义层，而是刻意剥离口径约束后的表/字段清单：
      同一份指标定义，去掉「默认口径、去重、除零保护、小样本」的强制约束后，
      模型还能不能写对？这就是 G0 vs G3 要量化的东西。
    """
    by_table: Dict[str, List[str]] = {}
    for m in (registry.get("measures") or []):
        t = m.get("table") or "?"
        lines = by_table.setdefault(t, [])
        unit = m.get("unit") or ""
        lines.append(f"  - {m.get('expr')}  -- {m.get('name')}"
                     f"（agg={m.get('agg')}{'，单位=' + unit if unit else ''}）")
        for kind, label in (("date_basis", "日期口径"), ("dept_basis", "科室口径")):
            emap = (m.get(kind) or {}).get("expr_map") or {}
            if emap:
                pairs = ", ".join(f"{k}→{v}" for k, v in emap.items())
                lines.append(f"      {label}字段：{pairs}")
        if m.get("dedup_key"):
            lines.append(f"      dedup_key：{m['dedup_key']}")
    for d in (registry.get("dimensions") or []):
        if d.get("type") == "time":
            continue
        t = d.get("table")
        if not t:
            continue
        lines = by_table.setdefault(t, [])
        lines.append(f"  - {d.get('expr')}  -- {d.get('name')}（维度）")
        if d.get("label_expr"):
            lines.append(f"  - {d['label_expr']}  -- {d.get('name')}名称")
        if d.get("enum"):
            lines.append(f"      可取：{'/'.join(str(x) for x in d['enum'][:12])}")
    out = []
    for t in sorted(by_table):
        out.append(f"表 {t}：")
        out.extend(dict.fromkeys(by_table[t]))      # 去重保序
        out.append("")
    return "\n".join(out)


def _parse_llm_json(text: str) -> Dict[str, Any]:
    """从 LLM 输出中提取 JSON（容忍 markdown 代码块）。"""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise PipelineError(f"LLM 输出不是合法 JSON: {e}\n原文前 200 字: {text[:200]}")


# ────────────────────────────────────────────────────────────
# 患者级明细门禁（合规一票否决）
# ────────────────────────────────────────────────────────────
# 问数场景默认只出聚合结果。命中即拒绝，不做任何降级（不「先给聚合版」，
# 否则等于告诉用户可以反复试探）。
PATIENT_LEVEL_PATTERNS = [
    ("患者名单", "患者名单"), ("病人名单", "病人名单"), ("患者清单", "患者清单"),
    ("患者明细", "患者明细"), ("病人明细", "病人明细"), ("明细数据", "明细数据"),
    ("住院号", "住院号"), ("病案号", "病案号"), ("身份证", "身份证"),
    ("患者姓名", "患者姓名"), ("叫什么名字", "具体患者身份"),
    ("手机号", "手机号"), ("联系方式", "联系方式"),
    ("哪些患者", "具体患者身份"), ("哪个患者", "具体患者身份"),
    ("患者级", "患者级数据"), ("个人病历", "个人病历"),
]


def detect_patient_level(question: str) -> Optional[str]:
    """命中患者级明细请求则返回拒绝理由，否则 None。

    ★ 只按「请求形态」判定，不按意图判定：
      意图分类可能把它判成 out_of_scope，但合规上必须记为 denied 并留痕，
      不能只回一句「暂不支持」了事。
    """
    for pat, reason in PATIENT_LEVEL_PATTERNS:
        if pat in question:
            return reason
    return None


# ────────────────────────────────────────────────────────────
# 越权预检（不依赖 LLM）
# ────────────────────────────────────────────────────────────
def detect_out_of_scope_dept(question: str, registry: Dict[str, Any],
                             dept_scope: Sequence[str]) -> Optional[str]:
    """问题里点名的科室不在数据范围内 → 直接拒绝。

    ★ 为什么必须在 LLM 之前再拦一道：
      实测中越权用例（G0003 呼吸科主任问心内科收入）出现过「LLM 生成失败 → error」
      而不是 denied。虽然没有出数，但合规上这不是一次明确的拒绝，
      而且判定结果会随模型输出漂移 —— 安全边界不能建立在模型的稳定性上。
    """
    scope = set(dept_scope or [])
    # ★ 空范围 = 无授权 → 直接拒绝（v0.9 修正）。
    #   此前 `if not scope` 与「全院」一起返回 None，等于把「无授权」当成「无限制」。
    if not scope:
        return "您没有任何数据范围授权，请联系数据管理办公室开通"
    if "*" in scope:
        return None
    dim = _index(registry.get("dimensions")).get("dept") or {}
    emap = dim.get("enum_map") or {}
    hits = {code for name, code in emap.items() if str(name) in question}
    bad = sorted(c for c in hits if c not in scope)
    if bad:
        return f"无权查看科室 {bad} 的数据（您的数据范围：{sorted(scope)}）"
    return None


def detect_out_of_scope_campus(question: str, registry: Dict[str, Any],
                               campus_scope: Optional[Sequence[str]]) -> Optional[str]:
    """问题里点名了数据范围外的院区 → 直接拒绝。

    ★ 为什么不能只靠 SQL 围栏（sql_builder 已按 campus_code 注入 RLS）：
      围栏会把结果**静默收口到你有权的院区**，于是「问东院」在只有西院权限时
      会返回西院的数字。用户读到的是「东院就是这个数」——不报错、不越权，
      但答案是错的。这类错误比越权更难发现，必须在预检阶段拦掉。

    ★ 院区字典（院区名 → campus_code）来自语义层 `dimensions[campus].enum_map`：
      配了就预检；**没配就跳过**（SQL 围栏照旧生效，只是不会提前给拒绝理由）。
      院区码是院方口径，不猜（docs/12 §3.5）。

    ★ 三种取值语义（与 dept_scope 对齐，但多一个"未启用"）：
      None = 未启用院区围栏（角色/用户都没配 campus_scope）→ 不检查，保持既有行为；
      []   = 显式无授权 → 拒绝；
      ["*"] = 全院区。
    为什么 None 与 [] 必须区分：`ask()` 的默认值只能是 None，
    否则所有既有调用方（含引擎自测）会被无差别拒绝。
    """
    if campus_scope is None:
        return None
    scope = set(campus_scope)
    if not scope:
        return "您没有任何院区范围授权，请联系数据管理办公室开通"
    if "*" in scope:
        return None
    dim = _index(registry.get("dimensions")).get("campus") or {}
    emap = dim.get("enum_map") or {}
    if not emap:
        return None                      # 字典未配置 → 不预检（不猜院名）
    hits = {code for name, code in emap.items() if str(name) in question}
    bad = sorted(c for c in hits if c not in scope)
    if bad:
        names = sorted(n for n, c in emap.items() if c in bad)
        return f"无权查看院区 {'、'.join(names)} 的数据（您的院区范围：{sorted(scope)}）"
    return None


# ────────────────────────────────────────────────────────────
# 权限门禁
# ────────────────────────────────────────────────────────────
def check_permission(dsl: QueryDSL, registry: Dict[str, Any],
                     role: str, dept_scope: Sequence[str],
                     campus_scope: Optional[Sequence[str]] = None) -> Optional[str]:
    """返回 None 表示通过，否则返回拒绝原因（不返回任何数值）。"""
    scope = set(dept_scope or [])
    # ★ 空范围 = 无授权（v0.9 修正）：不得因「没有科室 filter」就整条放行。
    if not scope:
        return "您没有任何数据范围授权，请联系数据管理办公室开通"
    wildcard = "*" in scope

    # 1) 科室越权
    for f in dsl.filters:
        if f.field in ("dept", "dept_code") and not wildcard:
            vals = f.value if isinstance(f.value, list) else [f.value]
            if f.op in ("=", "in") and any(str(v) not in scope for v in vals):
                return f"无权查看科室 {vals} 的数据（您的数据范围：{sorted(scope)}）"
            if f.op in ("!=", "not_in"):
                return "不支持跨科室排除式查询，请直接指定可查看的科室"

    # 1.5) 院区越权（语义与科室对齐；None = 未启用院区围栏，[] = 无授权）
    if campus_scope is not None:
        cscope = set(campus_scope)
        if not cscope:
            return "您没有任何院区范围授权，请联系数据管理办公室开通"
        if "*" not in cscope:
            for f in dsl.filters:
                if f.field in ("campus", "campus_code"):
                    vals = f.value if isinstance(f.value, list) else [f.value]
                    if f.op in ("=", "in") and any(str(v) not in cscope for v in vals):
                        return (f"无权查看院区 {vals} 的数据"
                                f"（您的院区范围：{sorted(cscope)}）")
                    if f.op in ("!=", "not_in"):
                        return "不支持跨院区排除式查询，请直接指定可查看的院区"

    # 2) restricted 维度（医师等涉及个人绩效）
    #    registry 中 dimensions 是 list；此处只读地建索引，不修改共享状态
    #    （曾因就地转成 dict 导致 sql_builder 迭代出字符串而崩溃）
    # ★ 用 permission.restricted_dimension_roles，而不是 restricted_metrics_roles：
    #    复用指标角色表会让「维度越权」检查形同虚设（有指标权限 ≠ 能下钻到个人）。
    dim_meta = _index(registry.get("dimensions"))
    restricted_roles = ((registry.get("permission") or {})
                        .get("restricted_dimension_roles") or [])
    for d in dsl.dimensions:
        meta = dim_meta.get(d)
        if meta and meta.get("sensitivity") == "restricted":
            if role not in restricted_roles:
                return f"维度「{meta.get('name', d)}」涉及个人绩效，当前角色无权下钻"
    return None


# ────────────────────────────────────────────────────────────
# 结果校验
# ────────────────────────────────────────────────────────────
def post_check(rows: Optional[List[Dict[str, Any]]], dsl: QueryDSL,
               min_cell: int = 5) -> List[str]:
    """执行后的结果校验：空结果 / 小样本抑制 / 对比期缺失。"""
    w: List[str] = []
    if rows is None:
        return w
    if not rows:
        w.append("查询结果为空：可能是该时间段无数据，或口径组合过窄。请勿据此判断为 0。")
        return w

    # 小样本抑制（防患者识别）
    suppressed = 0
    for r in rows:
        for k, v in r.items():
            if isinstance(v, (int, float)) and k.endswith("_cnt") and v < min_cell:
                suppressed += 1
                break
    if suppressed:
        w.append(f"{suppressed} 行样本量低于 {min_cell}，已抑制（前端应灰显），"
                 "防止下钻识别到患者")

    # 对比期校验（v0.15 修正）
    # ★ 被替换掉的旧写法：找 *_yoy / *_mom 列判断"变动超 50%"—— 但 SQL 生成器
    #   从不产出这种列（`_where` 只是把两期用 OR 并起来、按时间分组），
    #   所以那段检查从来没生效过，属于"写了但永不成立"的假门禁。
    #   真正危险的误读是：对比期一行数据都没有，用户把当期当成"同比 -100%"。
    if dsl.time.compare != "none" and rows:
        # 两期必须有**两个年份**（按月看同比是 2022-xx 与 2023-xx），
        # 只出现一个年份就说明对比期没有数据行。
        years = set()
        for r in rows:
            for tok in str(r.get("stat_period") or "").replace("/", "-").split("-"):
                if len(tok) == 4 and tok.isdigit():
                    years.add(tok)
                    break
        if len(years) < 2:
            w.append(f"本次带了 {dsl.time.compare} 对比，但结果里只覆盖 1 个年份"
                     f"（{'、'.join(sorted(years)) or '未知'}）：对比期在本院数据里没有记录，"
                     f"不能据此算同比/环比（这不代表变动 100%）")
    return w


# ────────────────────────────────────────────────────────────
# 管线
# ────────────────────────────────────────────────────────────
class AskPipeline:

    def __init__(self, registry: Dict[str, Any],
                 ambiguity: Optional[Dict[str, Any]] = None,
                 llm: Optional[LLMAdapter] = None,
                 executor: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
                 today: Optional[dt.date] = None,
                 max_repair: int = 2,
                 enable_ambiguity: bool = True,
                 enable_intent_hint: bool = True,
                 enable_fewshot: bool = False,
                 examples_path: Optional[str] = None,
                 dialect: str = "doris"):
        """开关用于消融实验（POC-B 的 G1/G2/G3 对比）：
           enable_ambiguity=False → 不做歧义反问（G1/G2）
           enable_intent_hint=False → 不做意图规则预判（G1/G2）
           enable_fewshot=True → 注入 3~5 条示例（G2）

        ★ few-shot 默认关闭：它是「待验证的增益」，不是「已证明的收益」。
          默认开启等于用生产流量做实验；先用 run_ablation 跑出 G1 vs G2，
          再决定是否通过 ASK_FEWSHOT=1 打开。
        """
        self.registry = registry
        self.ambiguity = ambiguity or {}
        self.llm = llm
        self.executor = executor
        self.today = today or dt.date.today()
        self.max_repair = max_repair
        self.enable_ambiguity = enable_ambiguity
        self.enable_intent_hint = enable_intent_hint
        self.enable_fewshot = enable_fewshot
        self._examples = (ExampleIndex(load_examples(examples_path), registry)
                          if enable_fewshot else None)
        self.builder = SQLBuilder(registry, dialect=dialect)  # 方言默认 doris，HIS 镜像传 oracle
        self.metrics = _index(registry.get("metrics"))

    # ── 主入口 ──
    def ask(self, question: str, role: str = "guest",
            dept_scope: Sequence[str] = ("*",),
            campus_scope: Optional[Sequence[str]] = None,
            resolved: Optional[Dict[str, str]] = None,
            inject_dsl: Optional[Dict[str, Any]] = None,
            context: Optional[Dict[str, Any]] = None) -> AskResult:
        res = AskResult(status="error", question=question)

        # 0.1) 多轮上文（F1.2 指代消解）：只在**同一会话内**由上层传入，
        #      内容是上一轮实际执行的口径（有效 DSL，不是模型原始输出）。
        #      ★ 这里只做两件事：把上文交给模型 + 留痕。**不代替模型做字段合并** ——
        #        引擎层的强制合并（"那X科呢" → 只换过滤）留待 C13，见 docs/13 §7。
        ctx = _norm_context(context)
        if ctx:
            res.step("context_in", {"prev_question": ctx.get("question"),
                                    "prev_metric": (ctx.get("dsl") or {}).get("metrics")})

        # 0) ★ 合规硬门禁：患者级明细请求一律拒绝，不进 LLM、不生成任何 SQL
        #     （放在最前，避免为一次注定被拒的请求消耗模型调用与审计噪声）
        pl = detect_patient_level(question)
        if pl:
            res.status = "denied"
            res.answer = (f"无权提供：{pl}。"
                          "问数场景默认只出聚合结果，患者级明细需单独申请、审批、"
                          "加水印并全程审计，请走病案/科研数据申请流程。")
            # ★ 早退分支也要留痕：否则评估器拿不到 DSL，
            #   「正确拒绝」会被判成生成失败（实测已发生）
            res.raw_dsl = {"intent": "out_of_scope"}
            res.step("patient_level_guard", pl)
            return res

        # 0.5) ★ 越权预检：点名了范围外科室 → 直接拒绝（不依赖 LLM，也不会因
        #      模型生成失败而退化成 error）
        pre = detect_out_of_scope_dept(question, self.registry, dept_scope)
        if pre:
            res.status = "denied"
            res.answer = f"无权访问：{pre}。如需查看请向数据管理办公室申请授权。"
            res.raw_dsl = {"intent": "out_of_scope"}   # 拒绝出数，留痕供评估
            res.step("permission_precheck", "denied")
            return res

        # 0.6) ★ 院区预检：点名了范围外院区 → 直接拒绝
        #      不靠 SQL 围栏兜底：围栏会把结果静默收口到你的院区，
        #      于是「问东院」会返回西院的数字 —— 不报错，但答案是错的。
        pre_campus = detect_out_of_scope_campus(question, self.registry, campus_scope)
        if pre_campus:
            res.status = "denied"
            res.answer = f"无权访问：{pre_campus}。如需查看请向数据管理办公室申请授权。"
            res.raw_dsl = {"intent": "out_of_scope"}
            res.step("permission_precheck", "denied_campus")
            return res

        # 1) 指标召回
        cands = recall_metrics(question, self.registry)
        res.step("recall", [c["code"] for c in cands])

        # 2) LLM 生成 DSL（含回炉修正）
        if inject_dsl is not None:
            raw = inject_dsl
        else:
            if self.llm is None:
                raise PipelineError("未配置 LLM 适配器")
            ex_block = ""
            if self._examples is not None:
                sel = self._examples.select(question, cands,
                                            predict_intent(question),
                                            predict_dimensions(question))
                ex_block = self._examples.render(sel)
                # 留痕：示例是"注入的实验条件"，评估时必须能还原
                res.step("fewshot", [{"id": e.get("id"), "why": e.get("_why")}
                                     for e in sel])
            prompt = build_prompt(question, cands, role, dept_scope,
                                  self.registry.get("dimensions"),
                                  intent_hint=self.enable_intent_hint,
                                  context=ctx, examples=ex_block)
            hint: Optional[List[str]] = None
            raw = None
            for attempt in range(self.max_repair + 1):
                raw = self.llm.generate(prompt, hint)
                errs = self._validate_raw(raw)
                if not errs:
                    break
                hint = errs
                res.step("repair", {"attempt": attempt + 1, "errors": errs})
                raw = None
            if raw is None:
                res.status = "error"
                res.answer = (f"已尝试 {self.max_repair + 1} 次仍无法生成合法查询，"
                              f"系统不会猜测您的口径。最近一次校验错误：{'; '.join(hint or [])}")
                res.warnings.append("低置信度，已拒绝出数（禁止编造）")
                return res

        # 3) 构造 + 校验 DSL
        try:
            dsl = dsl_from_dict(raw, self.registry)
        except Exception as e:
            res.status = "error"
            res.answer = f"DSL 解析失败：{e}"
            return res
        errs = validate_dsl(dsl, self.registry)
        if errs:
            res.status = "error"
            res.answer = "DSL 校验未通过：" + "; ".join(errs)
            return res
        res.dsl = dsl
        res.raw_dsl = raw

        # ★ 多指标静默丢弃是同一类事故（用户以为两个指标都算了，其实只算了第一个）：
        #   本版本的 SQL 生成器只取 metrics[0]，所以必须说出来，不许静默。
        if len(dsl.metrics) > 1:
            res.warnings.append(
                f"本版本一次只算一个指标，本次按「{dsl.metrics[0]}」出数；"
                f"问题里的其余指标未参与计算：{'、'.join(dsl.metrics[1:])}"
                f"（多指标组合见 docs/11 F3.6，尚未实现）")

        # 4) 元问题 / 超范围（这两类不反问口径，也不做权限外的数据输出）
        if dsl.intent == "meta_question":
            return self._answer_meta(dsl, res)
        if dsl.intent in ("need_clarify", "out_of_scope", "unsupported_metric"):
            res.status = "need_clarify" if dsl.intent == "need_clarify" else "out_of_scope"
            res.answer = (dsl.ask or dsl.suggest
                          or self._render_out_of_scope(dsl, cands))
            if res.status == "need_clarify":
                # 模型主动追问（无选项表）：不编选项，只把问题交给前端画卡片。
                res.clarify = [{"id": "ASK", "name": "需要补充信息",
                                "ambiguity": None, "ask": res.answer,
                                "options": [], "default": None,
                                "remember": False, "note": None}]
            res.step("out_of_scope", dsl.metrics)
            return res

        # 4.5) ★ 口径写回（docs/13 §7 C9）：把用户澄清的选项真正落到 DSL 上。
        #      必须早于权限门禁 —— 口径可能换掉指标（如「门诊量口径」换 op_visit_cnt），
        #      权限要按【最终】的指标判定，否则等于放行了一个没做过权限检查的指标。
        if dsl.intent in ASKABLE_INTENTS and resolved:
            trial = copy.deepcopy(dsl)
            applied, unapplied = apply_resolved(trial, resolved, self.ambiguity)
            if applied:
                errs2 = validate_dsl(trial, self.registry)
                if errs2:
                    # 语义层声明的映射与当前指标不兼容（如给门诊指标选了「出院日期」）：
                    # 整批回退，并要求用户重新确认 —— 不许"应用失败还照出数"。
                    unapplied = unapplied + [
                        {"id": a["id"], "name": a.get("name"), "choice": a["choice"],
                         "options": a.get("options") or [],
                         "reason": "该口径不适用于当前指标：" + "；".join(errs2)}
                        for a in applied]
                    applied = []
                else:
                    dsl = trial
                    res.dsl = dsl
                    res.step("apply_resolved",
                             [f"{a['id']}={a['choice']}→{a['field']}" for a in applied])
            if unapplied:
                rules_by_id = {r.get("id"): r for r in (self.ambiguity.get("rules") or [])}
                res.status = "need_clarify"
                res.answer = self._render_apply_blocked(unapplied)
                res.clarify = [clarify_item(rules_by_id[u["id"]])
                               for u in unapplied if u["id"] in rules_by_id]
                res.step("apply_resolved_blocked", [u["id"] for u in unapplied])
                return res

        # 5) 权限门禁 —— ★ 必须早于反问：
        #    越权请求若先被反问，会变成 need_clarify 而漏放，
        #    且反问本身泄露了「这份数据存在」。
        deny = check_permission(dsl, self.registry, role, dept_scope, campus_scope)
        if deny:
            res.status = "denied"
            res.answer = f"无权访问：{deny}。如需查看请向数据管理办公室申请授权。"
            res.step("permission", "denied")
            return res
        res.step("permission", "allowed")

        # 6) 歧义反问（此时已确认「有权看 + 确实要查数」）
        if dsl.intent in ASKABLE_INTENTS and self.enable_ambiguity:
            amb = detect_ambiguity(question, self.ambiguity, dsl, self.registry,
                                   resolved, self.ambiguity.get("max_asks_per_turn", 2))
            if amb:
                res.status = "need_clarify"
                res.answer = self._render_clarify(amb)
                res.clarify = [clarify_item(a) for a in amb]
                res.step("ambiguity", [a["id"] for a in amb])
                return res

        # 7) 生成 SQL
        ctx = {"role": role, "dept_scope": list(dept_scope)}
        if campus_scope is not None:
            # ★ 只有显式启用时才写入：sql_builder 看到 None/空都会跳过，
            #   但显式写入能让 SQL 生成层的 trace 看得出"这是被授权过的围栏"。
            ctx["campus_scope"] = list(campus_scope)
        try:
            built = self.builder.build(dsl, ctx=ctx, today=self.today)
        except DSLError as e:
            res.status = "error"
            res.answer = f"SQL 生成失败（口径不合法）：{e}"
            return res
        res.sql = built["sql"]
        res.caliber = built.get("caliber")
        res.warnings.extend(built.get("warnings") or [])
        res.step("sql", built["sql"][:120] + "...")

        # 8) 执行
        if self.executor is None:
            res.status = "answered"
            res.answer = self._render_no_executor(res)
            return res
        try:
            rows = self.executor(res.sql)
        except Exception as e:
            res.status = "error"
            res.answer = f"查询执行失败：{e}"
            return res
        res.rows = rows
        res.warnings.extend(post_check(rows, dsl))
        res.status = "answered"
        res.answer = self._render_answer(res, dsl)
        return res

    # ── 校验（生成期，允许回炉）──
    def _validate_raw(self, raw: Any) -> List[str]:
        if not isinstance(raw, dict):
            return ["输出必须是 JSON 对象"]
        # 先翻译模型方言（intent 别名 / 时间别名 / 中文枚举值），再判定，
        # 否则会把「说得不一样但意思正确」的模型判成错误，回炉也纠不回来。
        raw = normalize_dsl_dict(raw, self.registry)
        errs: List[str] = []
        if "sql" in raw:
            errs.append("禁止输出 sql 字段：只允许输出 DSL，SQL 由语义层生成")
        if "intent" not in raw:
            errs.append("缺少 intent 字段")
        elif raw.get("intent") not in ALLOWED_INTENTS:
            errs.append(f"intent '{raw.get('intent')}' 不在受控枚举内"
                        f"（可用：{', '.join(ALLOWED_INTENTS)}）")
        # ★ 时间范围必须显式声明：
        #   dsl_from_dict 会兜底成 last_month，若不在这里拦，模型「忘了给时间」
        #   也能出数 —— 用户拿到的就是一个口径不明的数字，这是最危险的静默错误。
        if raw.get("intent") in ASKABLE_INTENTS and not raw.get("time"):
            errs.append("缺少 time：必须明确时间范围，禁止使用默认时间")
        try:
            dsl = dsl_from_dict(raw, self.registry)
        except Exception as e:
            return errs + [f"DSL 结构错误：{e}"]
        errs.extend(validate_dsl(dsl, self.registry))
        return errs

    # ── 渲染 ──
    def _render_apply_blocked(self, unapplied: List[Dict[str, Any]]) -> str:
        """口径选不了就说清为什么 —— 不许"选了就当没选"。
        宁可这次不出数（docs/11 §1：宁可说不会，不可编造）。
        """
        lines = ["您选择的以下口径，系统无法自动应用，本次不出数：", ""]
        for u in unapplied:
            lines.append(f"  [{u.get('id')}] {u.get('name') or ''}")
            lines.append(f"        您的选择：{u.get('choice')}")
            lines.append(f"        原因：{u.get('reason')}")
            if u.get("options"):
                lines.append(f"        可改选：{' / '.join(u['options'])}")
            lines.append("")
        lines.append("也可以按默认口径出数（界面上的「按默认口径出数」）。")
        return "\n".join(lines)

    def _render_clarify(self, amb: List[Dict[str, Any]]) -> str:
        lines = ["在回答之前需要确认口径（避免给您错误的数字）：", ""]
        for a in amb:
            lines.append(f"  [{a['id']}] {a.get('ask', a.get('ambiguity', ''))}")
            if a.get("options"):
                lines.append(f"        可选项：{' / '.join(a['options'])}")
            if a.get("default"):
                lines.append(f"        若不指定，默认按「{a['default']}」计算")
            lines.append("")
        return "\n".join(lines)

    def _render_out_of_scope(self, dsl: QueryDSL,
                             candidates: Optional[Sequence[Dict[str, Any]]] = None) -> str:
        """超范围/未上线指标：明确说清缺什么，禁止临时拼 SQL 或给估算值。"""
        unknown = [c for c in (dsl.metrics or []) if c not in self.metrics]
        if unknown:
            names = "、".join(f"「{c}」" for c in unknown)
            return (f"{names} 尚未在指标语义层注册，暂不支持问数。"
                    "系统不会临时拼 SQL 或给出估算值。\n"
                    "请通过「指标申请」提交需求（需明确业务定义、口径与责任科室），"
                    "上线后即可问数。")
        if candidates is not None and not candidates:
            # ★ 召回为空是"该指标没建"的强信号，必须把「申请入口」给到用户。
            #   只回一句"暂不支持"，用户不知道该找谁、也不知道缺的是数据口径还是功能。
            return ("没有找到与这个问题匹配的指标 —— 本院指标库尚未覆盖，"
                    "因此不会临时拼 SQL 或给出估算值。\n"
                    "请通过「指标申请」提交需求（需明确业务定义、计算口径与责任科室），"
                    "上线后即可问数。")
        return "该问题暂不支持，已记录待补充指标。"

    def _answer_meta(self, dsl: QueryDSL, res: AskResult) -> AskResult:
        parts = []
        for code in dsl.metrics:
            m = self.metrics.get(code)
            if not m:
                continue
            parts.append(f"【{m.get('name', code)}】（{code}）")
            if m.get("definition"):
                parts.append(f"  定义：{m['definition']}")
            if m.get("formula"):
                parts.append(f"  公式：{m['formula']}")
            if m.get("note"):
                parts.append(f"  本院口径备注：{m['note']}")
            parts.append("")
        res.status = "meta"
        res.answer = "\n".join(parts) if parts else "未找到该指标的定义。"
        res.step("meta", True)
        return res

    def _render_no_executor(self, res: AskResult) -> str:
        return ("[未配置执行器，仅展示生成的 SQL 与口径]\n"
                + self._caliber_text(res))

    def _caliber_text(self, res: AskResult) -> str:
        c = res.caliber or {}
        return ("\n".join(f"  {k}: {v}" for k, v in c.items())
                if c else "  （无口径信息）")

    def _render_answer(self, res: AskResult, dsl: QueryDSL) -> str:
        rows = res.rows or []
        lines = [f"共 {len(rows)} 行", "", self._caliber_text(res), ""]
        for r in rows[:10]:
            lines.append("  " + ", ".join(f"{k}={v}" for k, v in r.items()))
        if len(rows) > 10:
            lines.append(f"  ...（还有 {len(rows) - 10} 行）")
        return "\n".join(lines)


# ────────────────────────────────────────────────────────────
# Golden 用例 → DSL
# ────────────────────────────────────────────────────────────
def norm_time_range(tr: Any) -> Dict[str, Any]:
    """归一化标注里的 time_range。

    ★ 这里**不再做 +1 天**（v0.17 修正）。标注侧写的是业务习惯的【闭区间】
      [2025-01-01, 2025-12-31]，而 DSL 的对外契约现在也是【闭区间】
      （见 sql_builder.resolve_time_range 的边界语义说明），两者一致，直接透传。
      此前这里 +1、模型侧不 +1，两处语义不同 → 真模型问「2023年」会少算 12-31 一整天。
      闭 → 半开的转换现在只在 resolve_time_range 一处发生。
    """
    if isinstance(tr, (list, tuple)) and len(tr) == 2:
        start = dt.date.fromisoformat(str(tr[0])[:10])
        end = dt.date.fromisoformat(str(tr[1])[:10])
        return {"type": "absolute", "start": start.isoformat(), "end": end.isoformat()}
    return {"type": tr or "last_month"}


def dsl_from_golden(case: Dict[str, Any]) -> Dict[str, Any]:
    """把扁平的 golden 标注转成 LLM 应输出的 DSL dict。"""
    d: Dict[str, Any] = {
        "intent": case.get("intent", "query_metric"),
        "metrics": case.get("metrics") or [],
        "dimensions": case.get("dimensions") or [],
        "filters": case.get("filters") or [],
        "time": {
            "granularity": case.get("time_granularity") or "month",
            "range": norm_time_range(case.get("time_range")),
            "compare": case.get("compare") or "none",
            "date_basis": case.get("date_basis"),
        },
        "options": {
            "dept_basis": case.get("dept_basis"),
            "dedup": case.get("dedup"),
            "top_n": case.get("top_n"),
            "order_by": case.get("order_by"),
        },
        "confidence": 1.0,
    }
    return d


def load_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError:
        raise PipelineError("需要 pyyaml：pip install pyyaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def _default_paths() -> Tuple[str, str]:
    return (os.path.join(_ROOT, "semantic-layer", "metric_registry.yaml"),
            os.path.join(_ROOT, "semantic-layer", "ambiguity_rules.yaml"))


def selftest() -> bool:
    reg_path, amb_path = _default_paths()
    registry = load_yaml(reg_path)
    amb = load_yaml(amb_path)

    golden_path = os.path.join(_ROOT, "eval", "golden_set_sample.jsonl")
    cases = [json.loads(l) for l in open(golden_path, encoding="utf-8") if l.strip()]

    passed = True

    def check(name: str, cond: bool, detail: str = ""):
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    today = dt.date(2026, 10, 14)

    # ── 0. 指标召回回归（断词/泛化/同义词/空集兜底） ──
    #   召回错了，后面再准也没用；单独成段，失败时一眼能定位到召回而不是生成。
    recall_fails = _recall_selftest(registry)
    for f in recall_fails:
        check(f, False)
    if not recall_fails:
        print("[OK]   指标召回：断词 / 泛化 / 同义词 / 空集兜底 全部通过")

    # ── 0.2 Few-shot 示例库（G2 的实验条件；默认关闭） ──
    try:
        from fewshot import selftest as _fewshot_selftest
        fs_fails = _fewshot_selftest()
        for f in fs_fails:
            check(f, False)
        if not fs_fails:
            print("[OK]   Few-shot 示例库：合法性 / 防抄袭 / 选择策略 全部通过")
        _first = cases[0]
        r_fs = AskPipeline(registry, amb,
                           llm=MockOracleLLM(dsl_from_golden(_first)),
                           today=today, enable_fewshot=True).ask(
            _first["question"], role=_first.get("role", "院长"),
            dept_scope=_first.get("dept_scope") or ["*"])
        check("开启 few-shot 后 trace 记录示例 id（评估可还原注入条件）",
              any(t.get("step") == "fewshot" for t in r_fs.trace))
        check("开启 few-shot 不改变结果状态（提示词增强不应动判定逻辑）",
              r_fs.status == "answered")
    except Exception as e:  # noqa: BLE001
        check(f"Few-shot 自测异常: {e}", False)

    # ── 0.3 未上线指标：答复必须给「申请入口」，不能只说"暂不支持" ──
    #   G0016「床位使用率」就是这条路径：召回为空 → out_of_scope → 用户得知道找谁申请
    _oos = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
        "全院床位使用率现在多少", role="院长", dept_scope=["*"],
        inject_dsl={"intent": "out_of_scope", "metrics": []})
    check("未上线指标答复包含「指标申请」入口（否则用户不知道该找谁）",
          _oos.status == "out_of_scope" and "指标申请" in _oos.answer,
          f"status={_oos.status} answer={_oos.answer[:40]}")

    # ── 1. 完美 LLM 跑通全量 golden ──
    ok = mismatch = 0
    detail = []
    for c in cases:
        p = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today)
        dsl_dict = dsl_from_golden(c)
        r = p.ask(c["question"], role=c.get("role", "guest"),
                  dept_scope=c.get("dept_scope", ["*"]), inject_dsl=dsl_dict)
        expect = c.get("permission_expect", "allow")
        if c.get("requires_clarify"):
            good = r.status == "need_clarify"
        elif expect == "deny":
            # 越权一票否决：不得返回任何数值、排名，也不得反问
            good = r.status == "denied"
        elif c.get("intent") == "meta_question":
            good = r.status == "meta"
        elif c.get("intent") == "out_of_scope":
            good = r.status == "out_of_scope" and not r.sql
        else:
            good = r.status in ("answered", "meta") and bool(r.sql)
        if good:
            ok += 1
        else:
            mismatch += 1
            detail.append(f"{c['id']}: status={r.status} answer={r.answer[:60]}")
    check(f"完美 LLM 端到端（{len(cases)} 条）", mismatch == 0,
          f"通过 {ok}/{len(cases)}" + ("; " + "; ".join(detail[:3]) if detail else ""))

    # ── 2. 会犯错的 LLM：每种错误都必须被拦住 ──
    base = dsl_from_golden(cases[0])
    escaped = []
    # ★ 用「科室主任」而非「院长」：院长在 restricted_dimension_roles 内，
    #   用它测 doctor 维度越权等于没测（这正是 v1.0 漏放的根因）
    probe_role = "科室主任"
    for fname in FAULT_INJECTORS:
        for repair in (False,):   # 不修正 → 必须被拦
            p = AskPipeline(registry, amb, llm=PerturbedLLM(base, [fname], repair), today=today)
            r = p.ask(cases[0]["question"], role=probe_role, dept_scope=["*"])
            if r.status == "answered" and r.sql:
                escaped.append(f"{fname}→放行")
    check(f"错误 LLM 全部被拦截（{len(FAULT_INJECTORS)} 种）", not escaped,
          "、".join(escaped) if escaped else "0 种被放行")

    # ── 3. 回炉修正机制 ──
    p = AskPipeline(registry, amb,
                    llm=PerturbedLLM(base, ["bad_date_basis"], repair_on_hint=True),
                    today=today)
    r = p.ask(cases[0]["question"], role="院长", dept_scope=["*"])
    check("回炉修正：第 2 次能自愈", r.status == "answered" and bool(r.sql),
          f"status={r.status}")

    # ── 4. 回炉次数上限 ──
    p = AskPipeline(registry, amb, llm=PerturbedLLM(base, ["bad_date_basis"]), today=today)
    r = p.ask(cases[0]["question"], role="院长", dept_scope=["*"])

    check("回炉上限：不无限重试且拒绝出数",
          r.status == "error" and "不会猜测" in r.answer, f"status={r.status}")

    # ── 5. 越权：不得返回任何数值 ──
    deny_cases = [c for c in cases if c.get("permission_expect") == "deny"]
    leaked = [c["id"] for c in deny_cases
              if AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today)
              .ask(c["question"], role=c.get("role"), dept_scope=c.get("dept_scope", ["*"]),
                   inject_dsl=dsl_from_golden(c)).status != "denied"]
    check(f"越权用例全部拒绝（{len(deny_cases)} 条）", not leaked,
          "漏放：" + ", ".join(leaked) if leaked else "漏放 0")

    # ── 5.5 ★ 空数据范围必须拒绝（空 ≠ 无限）v0.9 修正 ──
    #    背景：dept_scope 为空曾被 `or ["*"]` 回退成通配符，使 guest 可出全院
    #    internal 指标（越权预检 / 科室检查 / RLS 三处同时失效，见 docs/12 §1.2 S2）。
    #    这里做 A/B 对照：同一问题在合法范围下必须能出数，空范围必须 denied。
    allow_cases = [c for c in cases
                   if c.get("permission_expect", "allow") == "allow"
                   and not c.get("requires_clarify")
                   and c.get("intent") not in ("meta_question", "out_of_scope")]
    probe = allow_cases[0]
    ctrl = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
        probe["question"], role="院长", dept_scope=["*"], inject_dsl=dsl_from_golden(probe))
    check("对照：同一问题在合法范围下可出数",
          ctrl.status == "answered" and bool(ctrl.sql), f"status={ctrl.status}")

    r = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
        probe["question"], role="guest", dept_scope=[], inject_dsl=dsl_from_golden(probe))
    check("空数据范围必须拒绝（不得回退为全院）",
          r.status == "denied" and not r.rows and not r.sql, f"status={r.status}")

    # 纵深防御：即使绕过管线门禁，SQL 层也必须拒绝空范围
    try:
        SQLBuilder(registry, dialect="doris").build(
            dsl_from_dict(dsl_from_golden(probe), registry),
            ctx={"role": "guest", "dept_scope": []}, today=today)
        sql_guard = False
    except DSLError:
        sql_guard = True
    check("SQL 层兜底：空范围拒绝生成 SQL", sql_guard)

    # ── 6. 歧义反问 ──
    amb_cases = [c for c in cases if c.get("requires_clarify")]
    missed = [c["id"] for c in amb_cases
              if AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today)
              .ask(c["question"], role=c.get("role"), dept_scope=c.get("dept_scope", ["*"]),
                   inject_dsl=dsl_from_golden(c)).status != "need_clarify"]
    check(f"歧义用例全部反问（{len(amb_cases)} 条）", not missed,
          "漏反问：" + ", ".join(missed) if missed else "")

    # ── 6.1 ★ 反问必须带**结构化选项**（docs/13 §7 C1）──
    #   契约：UI 要画卡片（选项按钮 + 默认值 + 为什么问），
    #   就不能只依赖 answer 里那段给人看的文字。
    bad_ui = []
    for c in amb_cases:
        r = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
            c["question"], role=c.get("role"), dept_scope=c.get("dept_scope", ["*"]),
            inject_dsl=dsl_from_golden(c))
        ok = (r.status == "need_clarify" and r.clarify
              and all(x.get("id") for x in r.clarify)
              and any(x.get("options") for x in r.clarify))
        if not ok:
            bad_ui.append(f"{c['id']}→{r.status}/clarify={len(r.clarify)}")
    check(f"反问项结构化可渲染（{len(amb_cases)} 条：id + options）", not bad_ui,
          "不合格：" + ", ".join(bad_ui) if bad_ui else "")

    # 反向：出数/拒绝时不得留下反问项（否则前端会画出无意义的卡片）
    stray = []
    for c in cases:
        if c.get("requires_clarify"):
            continue
        r = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
            c["question"], role=c.get("role"), dept_scope=c.get("dept_scope", ["*"]),
            inject_dsl=dsl_from_golden(c))
        if r.status != "need_clarify" and r.clarify:
            stray.append(c["id"])
    check("非反问场景不残留反问项", not stray,
          "残留：" + ", ".join(stray[:5]) if stray else "")

    # ── 6.2 ★ 口径写回（docs/13 §7 C9）──
    #   反例（修前）：用户选了「结算日期」，管线只把这条规则标记为已澄清，
    #   DSL 上什么都没改 —— 出的是默认口径的数，界面却显示"已按所选口径重算"。
    #   这里把三件事钉住：① 声明了映射的必须真的改到 DSL；② 没声明映射的必须**拒绝出数**；
    #   ③ 映射表自身必须健康（路径合法、取值在枚举内、每个选项都被显式分类）。
    # ★ end 用闭区间（含 12-31）：与 DSL 对外契约一致（见 sql_builder.resolve_time_range）
    T = {"granularity": "year", "range": {"type": "absolute",
                                          "start": "2023-01-01", "end": "2023-12-31"}}

    def run_caliber(question, dsl_dict, resolved=None):
        return AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
            question, role="院长", dept_scope=["*"], resolved=resolved, inject_dsl=dsl_dict)

    rank_dsl = {"intent": "rank", "metrics": ["ip_los_avg"], "dimensions": ["dept"], "time": T}
    r = run_caliber("各科室平均住院日排名", rank_dsl, {"AMB-011": "最低前10"})
    check("口径写回：AMB-011 最低前10 → dir=asc + top_n=10",
          r.status == "answered" and r.dsl is not None
          and (r.dsl.options.order_by or {}).get("dir") == "asc"
          and r.dsl.options.top_n == 10 and "ASC" in (r.sql or ""),
          f"status={r.status} order_by={getattr(getattr(r, 'dsl', None), 'options', None) and r.dsl.options.order_by}")

    r = run_caliber("各科室平均住院日排名", rank_dsl, {"AMB-011": "全部排序"})
    check("口径写回：AMB-011 全部排序 → 去掉 top_n",
          r.status == "answered" and r.dsl is not None and not r.dsl.options.top_n,
          f"top_n={getattr(getattr(r, 'dsl', None), 'options', None) and r.dsl.options.top_n}")

    r = run_caliber("最近各科室平均住院日",
                    {"intent": "trend", "metrics": ["ip_los_avg"], "dimensions": ["dept"],
                     "time": {"granularity": "month", "range": {"type": "last_month"}}},
                    {"AMB-012": "最近3个月"})
    check("口径写回：AMB-012 最近3个月 → range=last_n_months n=3",
          r.status == "answered" and r.dsl is not None
          and (r.dsl.time.range or {}).get("type") == "last_n_months"
          and (r.dsl.time.range or {}).get("n") == 3,
          f"range={getattr(getattr(r, 'dsl', None), 'time', None) and r.dsl.time.range}")

    r = run_caliber("2023年出院人次环比",
                    {"intent": "compare", "metrics": ["ip_discharge_cnt"], "time": T},
                    {"AMB-013": "环比"})
    check("口径写回：AMB-013 环比 → time.compare=mom",
          r.status == "answered" and r.dsl is not None and r.dsl.time.compare == "mom",
          f"compare={getattr(getattr(r, 'dsl', None), 'time', None) and r.dsl.time.compare}")

    r = run_caliber("2023年心内科门诊人次是多少",
                    {"intent": "query_metric", "metrics": ["op_visit_cnt"],
                     "dimensions": ["dept"], "time": T},
                    {"AMB-004": "开单科室"})
    check("口径写回：AMB-004 开单科室 → options.dept_basis=order",
          r.status == "answered" and r.dsl is not None and r.dsl.options.dept_basis == "order",
          f"status={r.status} dept_basis={getattr(getattr(r, 'dsl', None), 'options', None) and r.dsl.options.dept_basis}")

    # ② 没声明映射 → 必须拒绝出数（不许按默认口径照出）
    r = run_caliber("2023年药占比是多少",
                    {"intent": "query_metric", "metrics": ["drug_rate"], "time": T},
                    {"AMB-003": "国家考核口径"})
    check("未声明映射：拒绝出数并说明原因（不得照默认口径出数）",
          r.status == "need_clarify" and not r.sql and "国家考核口径" in (r.answer or ""),
          f"status={r.status} sql={'有' if r.sql else '无'}")

    r = run_caliber("2023年门诊量是多少",
                    {"intent": "query_metric", "metrics": ["op_visit_cnt"], "time": T},
                    {"AMB-001": "挂号人次"})
    check("部分选项未声明：只拦该选项（挂号人次）",
          r.status == "need_clarify" and not r.sql and "挂号人次" in (r.answer or ""),
          f"status={r.status}")

    # ③ 选「默认值」= 无需改动，应正常出数（界面的「按默认口径出数」走这条）
    r = run_caliber("2023年药占比是多少",
                    {"intent": "query_metric", "metrics": ["drug_rate"], "time": T},
                    {"AMB-003": "院内口径（不含中草药，门诊+住院合并）"})
    check("选默认值 → 视为无需改动，正常出数",
          r.status == "answered" and bool(r.sql), f"status={r.status}")

    # ③b 声明为「等同默认口径」的说法（院方认可的同义词表）→ 正常出数
    r = run_caliber("2023年住院死亡率是多少",
                    {"intent": "query_metric", "metrics": ["inpatient_death_rate"], "time": T},
                    {"AMB-009": "住院期间死亡"})
    check("alias_to_default：同义说法（住院期间死亡=院内死亡）→ 正常出数",
          r.status == "answered" and bool(r.sql), f"status={r.status}")

    # ④ 声明的映射与指标不兼容 → 整批回退 + 说明（不许"应用失败还照出数"）
    r = run_caliber("2023年神经外科占用床日",
                    {"intent": "query_metric", "metrics": ["ip_occupy_bed_days"],
                     "dimensions": ["dept"], "time": T},
                    {"AMB-004": "执行科室"})   # 该指标 dept_basis 只支持 discharge / belong
    check("映射与指标不兼容 → 回退并说明，不出数",
          r.status == "need_clarify" and not r.sql and "不适用" in (r.answer or ""),
          f"status={r.status}")

    # ⑤ 映射表自检：路径合法 / 取值在枚举内 / 指标已注册 / 每个选项都被显式分类
    #    （最后一条正是 C9 的根因：选项没被分类，代码只好静默忽略）
    enum_of = {"time.date_basis": set(DateBasis.__args__),
               "options.dept_basis": set(DeptBasis.__args__),
               "options.dedup": set(Dedup.__args__),
               "time.compare": set(CompareType.__args__),
               "time.granularity": set(Granularity.__args__)}
    metric_codes = {m["code"] for m in (registry.get("metrics") or [])}
    bad_apply = []
    for rule in (amb.get("rules") or []):
        ap = rule.get("apply") or {}
        opts = list(rule.get("options") or [])
        if not opts:
            continue
        declared = (set((ap.get("values") or {}).keys()) | set((ap.get("set") or {}).keys())
                    | set(ap.get("alias_to_default") or [])
                    | set((ap.get("unresolvable_values") or {}).keys()))
        if not ap.get("unresolvable"):
            miss = [o for o in opts if o not in declared]
            if miss:
                bad_apply.append(f"{rule['id']} 选项未分类：{'、'.join(miss)}")
        pairs = [(o, ap.get("field"), v) for o, v in (ap.get("values") or {}).items()]
        for o, assigns in (ap.get("set") or {}).items():
            pairs += [(o, k, v) for k, v in assigns.items()]
        for o, path, val in pairs:
            if path not in APPLY_PATHS:
                bad_apply.append(f"{rule['id']}:{o} 路径 '{path}' 不合法")
            elif path == "metrics" and str(val) not in metric_codes:
                bad_apply.append(f"{rule['id']}:{o}→{val} 指标未注册")
            elif path in enum_of and str(val) not in enum_of[path]:
                bad_apply.append(f"{rule['id']}:{o}→{val} 不在 {path} 允许值内")
            elif path == "options.order_by" and not isinstance(val, dict):
                bad_apply.append(f"{rule['id']}:{o} order_by 必须是对象")
    check(f"歧义词表 apply 映射自检（{len(amb.get('rules') or [])} 条规则 × 选项全覆盖）",
          not bad_apply, "问题：" + "；".join(bad_apply[:5]) if bad_apply else "")

    # ── 7. 澄清后必须能出数（每条歧义用例，而不只是第一条）──
    #    ★ 只测「会不会反问」是不够的：反问 → 澄清 → 出数 是完整链路，
    #      任何一环断掉（澄清后又被别的规则反问、或 DSL 拼不出 SQL）都会让用户卡死。
    stuck = []
    for c in amb_cases:
        resolved = {c["clarify_rule_id"]: c.get("clarify_resolution", "")}
        r = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
            c["question"], role=c.get("role"), dept_scope=c.get("dept_scope", ["*"]),
            resolved=resolved, inject_dsl=dsl_from_golden(c))
        if not (r.status == "answered" and bool(r.sql)):
            stuck.append(f"{c['id']}:{r.status}")
    check(f"澄清后全部能出数（{len(amb_cases)} 条）", not stuck,
          "卡住：" + ", ".join(stuck) if stuck else "")

    # ── 8. 口径回显必须存在 ──
    r = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
        cases[0]["question"], role="院长", dept_scope=["*"],
        inject_dsl=dsl_from_golden(cases[0]))
    check("口径回显非空", bool(r.caliber), json.dumps(r.caliber, ensure_ascii=False)[:90])

    # ── 9. 结果校验：空结果不判 0 ──
    w = post_check([], dsl_from_dict(dsl_from_golden(cases[0])))
    check("空结果提示（不判为 0）", any("为空" in x for x in w), w[0] if w else "")

    # ── 9.5 ★ 对比期校验：只有 1 个年份时必须提示（否则用户会把当期当"同比 -100%"）──
    dsl_cmp = dsl_from_dict(dsl_from_golden(cases[0]))
    dsl_cmp.time.compare = "yoy"
    dsl_cmp.time.granularity = "month"
    w1 = post_check([{"stat_period": "2023-01", "ip_discharge_cnt": 10},
                     {"stat_period": "2023-02", "ip_discharge_cnt": 20}], dsl_cmp)
    check("对比期无数据（只覆盖 1 个年份）→ 明确提示，不许当同比 -100%",
          any("对比期" in x for x in w1), w1[0] if w1 else "（无警告）")
    w2 = post_check([{"stat_period": "2022-01", "ip_discharge_cnt": 10},
                     {"stat_period": "2023-01", "ip_discharge_cnt": 20}], dsl_cmp)
    check("两期都有数据 → 不误报", not any("对比期" in x for x in w2),
          "；".join(w2))

    # ── 10. 小样本抑制 ──
    w = post_check([{"dept": "A", "ip_discharge_cnt": 3}],
                   dsl_from_dict(dsl_from_golden(cases[0])))
    check("小样本抑制提示", any("抑制" in x for x in w), w[0] if w else "")

    # ── 11. 患者级请求：合规硬门禁 ──
    r = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
        "把上个月心衰住院的患者名单给我", role="科室主任", dept_scope=["CARD"],
        inject_dsl={"intent": "out_of_scope", "metrics": []})
    check("患者级明细一律拒绝（不因判成超范围而放行）", r.status == "denied",
          f"status={r.status}")

    # ── 12. echo_only 口径不得反问（AMB-005 教训）──
    r = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
        "2025年全年住院收入是多少", role="院长", dept_scope=["*"],
        inject_dsl={"intent": "query_metric", "metrics": ["ip_revenue"],
                    "dimensions": [], "filters": [],
                    "time": {"granularity": "year", "range": {"type": "last_year"},
                             "date_basis": "settle"}})
    check("已限定范围的收入 + echo_only 时间基准 → 直接出数",
          r.status == "answered" and bool(r.sql), f"status={r.status}")

    # ── 13. 时间范围不得静默取默认 ──
    p = AskPipeline(registry, amb,
                    llm=PerturbedLLM(dsl_from_golden(cases[0]), ["missing_time"]),
                    today=today)
    r = p.ask(cases[0]["question"], role="院长", dept_scope=["*"])
    check("LLM 漏给时间 → 拒绝出数（禁止默认时间）",
          r.status == "error" and "不会猜测" in r.answer, f"status={r.status}")

    # ── 14. 多轮上文（F1.2 指代消解）：上文必须进提示词，且必须可审计 ──
    #    ★ 关键点不是"传了参数"，而是：传了 → 模型看得见 + trace 有记录；
    #      没传 → 提示词里不许出现「对话上文」（否则模型会凭空指代，编出上一轮的口径）。
    ctx = {"question": "上个月全院出院人次是多少", "metric_code": "ip_discharge_cnt",
           "dsl": dsl_from_golden(cases[0])}
    p_in = build_prompt("那心内科呢", [], "医务处", ["*"], None, context=ctx)
    check("多轮上文进入提示词（模型据此做指代消解）",
          "对话上文" in p_in and "那心内科呢" in p_in
          and "上个月全院出院人次是多少" in p_in
          and "只改被问到的部分" in p_in)
    p_no = build_prompt("那心内科呢", [], "医务处", ["*"], None)
    check("未传上文时提示词里不出现「对话上文」（不假装有上下文）",
          "对话上文" not in p_no)
    check("空 DSL 的上文被丢弃（宁可从头解析，也不假装能指代）",
          _norm_context({"question": "x", "dsl": {}}) is None
          and _norm_context(None) is None
          and _norm_context({"dsl": {"metrics": ["ip_discharge_cnt"]}}) is not None)
    r = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
        "那心内科呢", role="院长", dept_scope=["*"],
        inject_dsl=dsl_from_golden(cases[0]), context=ctx)
    check("带上文时 trace 记录 context_in（可审计、可排查指代错误）",
          any(s["step"] == "context_in" for s in r.trace),
          f"status={r.status} trace={[s['step'] for s in r.trace]}")
    r2 = AskPipeline(registry, amb, llm=MockOracleLLM({}), today=today).ask(
        "那心内科呢", role="院长", dept_scope=["*"], inject_dsl=dsl_from_golden(cases[0]))
    check("不带上文时不记录 context_in（避免误认为继承了上文）",
          not any(s["step"] == "context_in" for s in r2.trace))

    # ── 15. 院区数据范围（F6.3）：预检 / 门禁 / SQL 围栏 三层都要生效 ──
    #    ★ 为什么必须验三层而不是只验 SQL：围栏只会把结果**静默收口**到有权的院区，
    #      于是「问东院」在只有西院权限时返回西院的数字 —— 不报错，但答案是错的。
    #      这类错误比越权更难发现，所以预检、门禁、围栏各验一遍。
    reg_campus = json.loads(json.dumps(registry))     # 深拷贝，别动共享的 registry
    for _d in reg_campus.get("dimensions") or []:
        if _d.get("code") == "campus":
            _d["enum_map"] = {"东院": "01", "西院": "02"}
    T15 = {"intent": "query_metric", "metrics": ["ip_discharge_cnt"], "dimensions": [],
           "time_range": ["2023-01-01", "2023-12-31"]}

    # ① 三态语义（None / [] / ["*"]）
    check("院区 scope=None → 未启用围栏（不检查，保持既有行为）",
          detect_out_of_scope_campus("2023年东院出院人次", reg_campus, None) is None)
    check("院区 scope=[] → 显式无授权，拒绝",
          bool(detect_out_of_scope_campus("2023年出院人次", reg_campus, [])))
    check('院区 scope=["*"] → 全院，放行',
          detect_out_of_scope_campus("2023年东院出院人次", reg_campus, ["*"]) is None)

    # ② 预检：点名范围外院区必须拒绝
    hit = detect_out_of_scope_campus("2023年东院出院人次", reg_campus, ["02"])
    check("点名范围外院区 → 直接拒绝（不靠围栏静默收口）",
          bool(hit) and "东院" in (hit or ""), hit or "")
    check("点名范围内院区 → 放行",
          detect_out_of_scope_campus("2023年东院出院人次", reg_campus, ["01"]) is None)
    check("语义层没配院区字典时不预检（不猜院名，围栏照旧生效）",
          detect_out_of_scope_campus("2023年东院出院人次", registry, ["02"]) is None)

    # ③ 门禁：DSL 里显式指定范围外院区也要拦
    dsl_campus = dsl_from_dict(dsl_from_golden(
        dict(T15, filters=[{"field": "campus", "op": "=", "value": "02"}])))
    _deny = check_permission(dsl_campus, reg_campus, "运营管理部", ["*"], ["01"])
    check("DSL 显式指定范围外院区 → 门禁拒绝",
          bool(_deny) and "院区" in (_deny or ""), _deny or "")
    check("DSL 指定范围内院区 → 放行",
          check_permission(dsl_campus, reg_campus, "运营管理部", ["*"], ["02"]) is None)

    # ④ SQL 围栏 + 回归保护
    _b = SQLBuilder(reg_campus, dialect="doris").build(
        dsl_from_dict(dsl_from_golden(T15)),
        ctx={"role": "运营管理部", "dept_scope": ["*"], "campus_scope": ["02"]}, today=today)
    check("SQL 层按院区收口（campus_code IN）", "campus_code IN ('02')" in _b["sql"])
    _b2 = SQLBuilder(reg_campus, dialect="doris").build(
        dsl_from_dict(dsl_from_golden(T15)),
        ctx={"role": "运营管理部", "dept_scope": ["*"]}, today=today)
    check("未启用院区围栏时 SQL 不变（升级不得悄悄改变数据范围）",
          "campus_code IN" not in _b2["sql"])

    # ⑤ 端到端：预检必须早于 LLM（不依赖模型输出，也就不会随模型漂移）
    r = AskPipeline(reg_campus, amb, llm=MockOracleLLM({}), today=today).ask(
        "2023年东院出院人次是多少", role="运营管理部", dept_scope=["*"],
        campus_scope=["02"], inject_dsl=dsl_from_golden(cases[0]))
    check("端到端：问范围外院区 → denied 且不出 SQL/数值",
          r.status == "denied" and not r.sql and not r.rows, f"status={r.status}")
    check("端到端：院区预检拒绝也留痕（可审计）",
          any(s["step"] == "permission_precheck" and s.get("detail") == "denied_campus"
              for s in r.trace), str([s.get("detail") for s in r.trace]))

    # ── 16. 绝对时间区间的边界（v0.17 修「少一天」）──
    #    ★ 契约：DSL 的 absolute.end = **含当天**的闭区间；SQL 用 [start, end)。
    #      修前：真模型按闭区间给 end=2023-12-31，引擎把它当开区间 →
    #      整年少算 12-31 一整天（2023 年出院人次 59,653 被算成 59,356），
    #      且不报错、界面上看着完全正常。这是「静默给错」的典型。
    #    ★ 修法：闭 → 半开的转换只保留 resolve_time_range 一处，两条链路契约一致。
    def _sql_of(d: Dict[str, Any]) -> str:
        return SQLBuilder(registry, dialect="doris").build(
            dsl_from_dict(d), today=today)["sql"] or ""

    _closed = {"intent": "query_metric", "metrics": ["ip_discharge_cnt"], "dimensions": [],
               "time": {"granularity": "year",
                        "range": {"type": "absolute",
                                  "start": "2023-01-01", "end": "2023-12-31"}}}
    _sql_model = _sql_of(_closed)
    check("绝对区间：闭区间 end=2023-12-31 → SQL 用 < 2024-01-01（覆盖整年）",
          "< '2024-01-01'" in _sql_model, "")
    check("绝对区间：不得出现少一天的 < 2023-12-31",
          "< '2023-12-31'" not in _sql_model)

    # 标注链路（golden 的闭区间列表）与模型链路（闭区间 dict）必须产出同一段 SQL
    #   （granularity 也要给一致，否则比的是 GROUP BY 而不是时间区间）
    _sql_golden = _sql_of(dsl_from_golden(
        {"intent": "query_metric", "metrics": ["ip_discharge_cnt"],
         "time_granularity": "year",
         "time_range": ["2023-01-01", "2023-12-31"]}))
    check("两条链路（模型闭区间 / 标注闭区间）的时间区间完全一致",
          _sql_golden == _sql_model,
          ("model=" + _sql_model[:0]) or "")

    # 单日区间：闭区间 [12-31, 12-31] 必须覆盖当天
    _sql_one_day = _sql_of({"intent": "query_metric", "metrics": ["ip_discharge_cnt"],
                            "dimensions": [],
                            "time": {"granularity": "day",
                                     "range": {"type": "absolute",
                                               "start": "2023-12-31", "end": "2023-12-31"}}})
    check("绝对区间：单日 2023-12-31 → [12-31, 01-01) 覆盖当天",
          ">= '2023-12-31'" in _sql_one_day and "< '2024-01-01'" in _sql_one_day,
          _sql_one_day[:0] or "")

    # 跨月边界（非自然月整段）：闭区间照样只 +1 天，不会多算
    _sql_mid = _sql_of({"intent": "query_metric", "metrics": ["ip_discharge_cnt"],
                        "dimensions": [],
                        "time": {"granularity": "month",
                                 "range": {"type": "absolute",
                                           "start": "2023-03-15", "end": "2023-06-30"}}})
    check("绝对区间：3-15 ~ 6-30 → [03-15, 07-01)（只 +1 天，不含 7 月）",
          ">= '2023-03-15'" in _sql_mid and "< '2023-07-01'" in _sql_mid)

    return passed


# ────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="端到端问数管线")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--ask", help="自然语言问题")
    ap.add_argument("--role", default="guest")
    ap.add_argument("--dept-scope", default="*")
    ap.add_argument("--llm", default="mock", choices=["mock", "http"])
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        print("=" * 68)
        print("端到端管线 自测")
        print("=" * 68)
        ok = selftest()
        print("=" * 68)
        print("自测结果: " + ("全部通过" if ok else "存在失败"))
        return 0 if ok else 1

    if not args.ask:
        ap.error("需要 --ask 或 --selftest")

    reg_path, amb_path = _default_paths()
    registry = load_yaml(reg_path)
    amb = load_yaml(amb_path)

    llm: LLMAdapter
    if args.llm == "http":
        llm = HTTPLLM()
    else:
        print("[提示] mock 模式无真实 LLM，请加 --llm http 并配置 "
              "ASK_LLM_BASE_URL / ASK_LLM_API_KEY（须为院内私有化部署）")
        return 2

    p = AskPipeline(registry, amb, llm=llm)
    r = p.ask(args.ask, role=args.role,
              dept_scope=[s.strip() for s in args.dept_scope.split(",")])
    print(json.dumps(r.as_dict(), ensure_ascii=False, indent=2)
          if args.verbose else r.answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
