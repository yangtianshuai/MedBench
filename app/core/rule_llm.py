# -*- coding: utf-8 -*-
"""无模型时的降级 NLU：用语义层已有的中文别名做规则匹配。

定位：
    内网尚未部署私有化模型时，保证「自然语言 → DSL → SQL → 出数」链路仍可演示。

⚠️ 它不是智能：只覆盖常见表达，复杂问题会落空或退化。
   生产环境必须把 ASK_LLM_BASE_URL 指向院内模型服务，届时本类不会被实例化。

为什么不返回空 DSL 了事：
    空实现会让界面上每个问题都失败，看起来像「系统坏了」，
    而真实原因只是「没装模型」——这两种情况必须能被区分开。
    本类至少让常见问法出数，同时 confidence 只给 0.6，不冒充模型判断。

多轮（F1.2）：`build_prompt` 在有上文时会把「上一轮实际执行的 DSL」写进提示词，
    本类据此做**有限的**指代消解（换粒度 / 换范围 / 加对比 / 指定或排除科室），
    识别不了的追问一律返回「超出范围」——**宁可说不会，也不猜一个口径出来**。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_ENGINE_DIR = Path(__file__).resolve().parent.parent.parent / "engine"
if str(_ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(_ENGINE_DIR))

from pipeline import LLMAdapter  # noqa: E402


def _question_of(prompt: str) -> str:
    """从 prompt 里取出用户问题（build_prompt 用【用户问题】标记）。"""
    m = re.search(r"【用户问题】(.*?)(?:\n【|\Z)", prompt, re.S)
    return (m.group(1) if m else "").strip()


class RuleBasedLLM(LLMAdapter):
    """基于语义层别名表的规则匹配 NLU。"""

    def __init__(self, registry: Dict[str, Any]):
        self.registry = registry
        self.metrics = registry.get("metrics") or []
        self.dims = registry.get("dimensions") or []

    # ── 指标：name/alias 最长匹配 ─────────────────────────
    def _pick_metric(self, q: str) -> Optional[Dict[str, Any]]:
        """取匹配到的最长别名对应指标。

        为什么按长度而不是按顺序：『出院人次』与『入院人次』
        只差一个字，长匹配能自然区分开；顺序匹配会随机取中。
        """
        best: Optional[Dict[str, Any]] = None
        best_len = 0
        for m in self.metrics:
            if m.get("_unmapped"):
                continue
            names = [m.get("name") or ""] + [str(a) for a in (m.get("alias") or [])]
            for nm in names:
                if nm and nm in q and len(nm) > best_len:
                    best, best_len = m, len(nm)
        return best

    # ── 维度 ──────────────────────────────────────────────
    def _pick_dims(self, q: str) -> List[str]:
        out: List[str] = []
        if re.search(r"各科室|按科室|分科室|科室排名|哪个科|各科", q):
            out.append("dept")
        if re.search(r"各院区|分院区|按院区|各分院", q):
            out.append("campus")
        for d in self.dims:
            code = d.get("code")
            if not code or code in out:
                continue
            names = [d.get("name") or ""] + [str(a) for a in (d.get("alias") or [])]
            if any(nm and re.search(rf"各{nm}|按{nm}|分{nm}", q) for nm in names):
                out.append(code)
        return out

    # ── 时间 ──────────────────────────────────────────────
    def _pick_time_explicit(self, q: str) -> Optional[Dict[str, Any]]:
        """只认**问题里明说的**时间；没说明就返回 None。

        ★ 与 `_pick_time` 分开的原因是多轮：
          追问「那心内科呢」里没有任何时间词，此时必须**继承上文的时间范围**，
          而不是当成"没说 → 默认上月"。把两者混在一起，
          就会让同一会话里的口径悄悄变掉（用户以为还在看 2023 年）。
        """
        if m := re.search(r"(20\d{2})\s*年", q):
            y = int(m.group(1))
            # ★ end 用**闭区间**（含 12-31）：与 DSL 对外契约一致，
            #   半开区间的转换由 sql_builder.resolve_time_range 一处完成。
            #   这里曾写 f"{y+1}-01-01"（自做开区间），改了契约后必须同步，否则会多算一天。
            return {"type": "absolute", "start": f"{y}-01-01", "end": f"{y}-12-31"}
        if m := re.search(r"(?:近|过去|最近)\s*(\d+)\s*个月", q):
            return {"type": "last_n_months", "n": int(m.group(1))}
        if re.search(r"去年|上一年", q):
            return {"type": "last_year"}
        if re.search(r"今年|本年度|年至今", q):
            return {"type": "year_to_date"}
        if re.search(r"上个月|上月", q):
            return {"type": "last_month"}
        if re.search(r"本月|这个月|当月", q):
            return {"type": "current_month"}
        if re.search(r"上季度", q):
            return {"type": "last_quarter"}
        if re.search(r"上周", q):
            return {"type": "week_before_last"}
        return None

    def _pick_time(self, q: str) -> Dict[str, Any]:
        # 兜底与本系统默认一致。注意：不编造绝对日期区间，
        # 宁可出「上月」这种明确区间，也不猜一个具体范围出来。
        return self._pick_time_explicit(q) or {"type": "last_month"}

    # ── 多轮上文（F1.2 指代消解）──────────────────────────
    def _context_dsl(self, prompt: str) -> Optional[Dict[str, Any]]:
        """从提示词里取「上一轮实际执行的 DSL」。

        ★ 这是与 engine/pipeline.py::build_prompt 的**文本契约**。
          取不到就返回 None —— 本类会退化回"不认识追问"，绝不瞎猜。
        """
        m = re.search(r"上一轮实际执行的 DSL：(\{[^\n]*\})", prompt)
        if not m:
            return None
        try:
            d = json.loads(m.group(1))
        except Exception:
            return None
        return d if isinstance(d, dict) and d.get("metrics") else None

    def _dept_terms(self) -> List[str]:
        for d in self.dims:
            if d.get("code") == "dept":
                return [str(x) for x in (d.get("enum") or [])]
        return []

    def _mentioned_dept(self, q: str) -> Optional[str]:
        """取问题里提到的科室（最长匹配：『心内科CCU』优于『心内科』）。

        口语里科室名常被省掉「科」字（『排除急诊』而不是『排除急诊科』），
        所以额外认一次去掉末尾「科」的说法；
        但**返回的始终是语义层枚举里的正式名** —— 过滤值必须能过 enum 校验。
        """
        best: Optional[str] = None
        for t in self._dept_terms():
            cands = {t}
            if t.endswith("科") and len(t) > 2:
                cands.add(t[:-1])
            for c in cands:
                if c in q and (best is None or len(c) > len(best)):
                    best = t
        return best

    def _followup(self, q: str, prev: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """按上文改写：换粒度 / 换范围 / 加对比 / 指定或排除科室。

        只做**能明确识别**的这几种，其余返回 None（上层报"超出范围"）。
        原则与首问一致：宁可说不会，也不编一个口径出来。
        """
        if not prev:
            return None
        dsl = json.loads(json.dumps(prev, ensure_ascii=False))
        time_ = dsl.setdefault("time", {})
        changed: List[str] = []

        for kw, g in (("按季度", "quarter"), ("按季", "quarter"), ("各季度", "quarter"),
                      ("按月", "month"), ("逐月", "month"), ("按年", "year"),
                      ("按周", "week"), ("按日", "day")):
            if kw in q:
                time_["granularity"] = g
                changed.append(f"粒度={g}")
                break
        if "同比" in q or "去年同期" in q:
            time_["compare"] = "yoy"
            changed.append("compare=yoy")
        elif "环比" in q:
            time_["compare"] = "mom"
            changed.append("compare=mom")

        if rng := self._pick_time_explicit(q):
            time_["range"] = rng
            changed.append(f"范围={rng.get('type')}")

        dept = self._mentioned_dept(q)
        if dept:
            neg = bool(re.search(r"排除|不含|去掉|不看", q))
            filters = [f for f in (dsl.get("filters") or []) if f.get("field") != "dept"]
            filters.append({"field": "dept", "op": "!=" if neg else "=", "value": dept})
            dsl["filters"] = filters
            changed.append(f"科室{'!=' if neg else '='}{dept}")

        if not changed:
            return None
        # 置信度刻意低于首问：这是"按上文改写"，不是重新理解了一遍问题
        dsl["confidence"] = 0.5
        return dsl

    # ── 时间粒度 ──────────────────────────────────────────
    def _pick_granularity(self, q: str) -> str:
        if re.search(r"各月|每月|按月|逐月", q):
            return "month"
        if re.search(r"各季度|按季度", q):
            return "quarter"
        if re.search(r"各年|逐年|按年", q):
            return "year"
        # 「2023 年出院人次」问的是一个数，不该摊成 12 行
        if re.search(r"(20\d{2})\s*年", q):
            return "year"
        return "month"

    # ── 入口 ──────────────────────────────────────────────
    def generate(self, prompt: str, hint: Optional[List[str]] = None) -> Dict[str, Any]:
        q = _question_of(prompt)
        if not q:
            return {}
        prev = self._context_dsl(prompt)
        metric = self._pick_metric(q)
        if not metric:
            # ★ 先试「按上文改写」（多轮追问），再判超范围。
            #   顺序不能反：『那心内科呢』『按季度看』本来就不是一个完整的问句，
            #   它们只有在有上文时才有意义。
            if fu := self._followup(q, prev):
                return fu
            # 匹配不到指标就明确说「超出范围」，不要瞎猜一个指标出来
            return {"intent": "out_of_scope", "metrics": [], "confidence": 0.3}

        # ★ 问「怎么算的」→ meta_question（只回口径，不查数）。
        #   生产环境由模型判断，降级模式也必须能识别：否则「口径说明卡」在界面上
        #   永远出不来，用户会把「查不到数」误读成「这个指标没有定义」（docs/13 §3.3）。
        if re.search(r"(怎么|如何|怎样)(算|计算|定义|统计|得出)|定义是什么|口径是什么|指标说明|怎么来的", q):
            return {"intent": "meta_question", "metrics": [metric["code"]], "confidence": 0.6}

        dims = self._pick_dims(q)
        prev_time = (prev or {}).get("time") or {}
        prev_opts = (prev or {}).get("options") or {}

        # 时间：问题明说就用明说的；没明说但上文有 → **沿用上文**（不是退回默认）；
        #       两者都没有才用系统默认。这一条是多轮口径不跑偏的关键。
        explicit = self._pick_time_explicit(q)
        if explicit:
            rng = explicit
        elif prev_time.get("range"):
            rng = prev_time["range"]
        else:
            rng = {"type": "last_month"}
        gran_hint = bool(re.search(
            r"各月|每月|按月|逐月|各季度|按季度|各年|逐年|按年|(20\d{2})\s*年", q))
        gran = (self._pick_granularity(q) if gran_hint
                else (prev_time.get("granularity") or self._pick_granularity(q)))
        compare = ("yoy" if ("同比" in q or "去年同期" in q)
                   else "mom" if "环比" in q else "none")

        time_out: Dict[str, Any] = {"granularity": gran, "range": rng, "compare": compare}
        opts: Dict[str, Any] = {}
        # 口径参数（时间基准 / 科室归属 / 去重）不是"问题的一部分"：
        # 同一会话里没被明确改掉就沿用，否则同一指标前后会变成两套口径。
        if prev_time.get("date_basis"):
            time_out["date_basis"] = prev_time["date_basis"]
        for k in ("dept_basis", "dedup"):
            if prev_opts.get(k):
                opts[k] = prev_opts[k]
        if m := re.search(r"前\s*(\d+)", q):
            opts["top_n"] = int(m.group(1))
        rank = bool(re.search(r"排名|排行|最高|最低|最多|最少|top", q, re.I))
        ascending = bool(re.search(r"最低|最少|最短|最小", q))
        intent = "rank" if (rank and dims) else "query_metric"
        if rank and dims:
            opts["order_by"] = {"metric": metric["code"],
                                "dir": "asc" if ascending else "desc"}

        return {
            "intent": intent,
            "metrics": [metric["code"]],
            "dimensions": dims,
            "filters": [],
            "time": time_out,
            "options": opts,
            "confidence": 0.6,   # 明确低于模型的置信度，不冒充
        }


# ────────────────────────────────────────────────────────────
# 自测：多轮继承必须"要么明确继承，要么明确不认"
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    """运行：python -m app.core.rule_llm --selftest"""
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from app.config import settings  # noqa: E402
    from app.core.registry import load_ambiguity, load_registry  # noqa: E402
    from pipeline import AskPipeline  # noqa: E402  （engine/pipeline.py）

    registry = load_registry()
    amb = load_ambiguity()
    llm = RuleBasedLLM(registry)
    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    def pipeline(enable_ambiguity: bool = True) -> AskPipeline:
        # 不接执行器：本自测只验「自然语言 → DSL → SQL」，不依赖任何数据库
        return AskPipeline(registry, amb, llm=RuleBasedLLM(registry),
                           executor=lambda sql: [], enable_ambiguity=enable_ambiguity,
                           dialect=settings.dialect)

    p = pipeline()
    first = p.ask("2023年全院出院人次是多少", role="院长", dept_scope=["*"])
    check("首问：规则 NLU 出数（降级模式必须有可用链路）", first.status == "answered",
          f"status={first.status}")
    prev = first.dsl.to_dict() if first.dsl else None
    check("首问：时间范围=提问里明说的 2023",
          bool(prev) and prev["time"]["range"].get("type") == "absolute",
          str((prev or {}).get("time", {}).get("range")))
    # ★ 绝对区间的 end 必须是**含当天**的闭区间（12-31），不能写成"下一天"（2024-01-01）：
    #   写成下一天会让整年少算 12-31（59,653 → 59,356），且不报错。
    check("首问：闭区间上界是 2023-12-31（不是 2024-01-01）",
          str((prev or {}).get("time", {}).get("range", {}).get("end", "")) == "2023-12-31",
          str((prev or {}).get("time", {}).get("range", {}).get("end")))
    # 断言不写成 `'2024-01-01'` 字面量：不同方言的日期写法不同
    # （doris → '2024-01-01'，oracle → TO_DATE('2024-01-01','YYYY-MM-DD')）
    check("首问：SQL 时间条件覆盖整年（上界到 2024-01-01、且不出现 2023-12-31）",
          "2024-01-01" in (first.sql or "") and "2023-12-31" not in (first.sql or ""),
          (first.sql or "")[:0] or "")
    ctx = {"question": "2023年全院出院人次是多少", "dsl": prev, "metric_code": "ip_discharge_cnt"}

    # ① 追问：只说科室 → 继承指标与时间，只加科室过滤
    fu = llm.generate("【用户问题】那心内科呢\n【对话上文】上一轮实际执行的 DSL："
                      + json.dumps(prev, ensure_ascii=False) + "\n【输出】")
    check("追问「那心内科呢」→ 指标与时间继承上文，只加科室过滤",
          fu.get("metrics") == ["ip_discharge_cnt"]
          and (fu.get("filters") or [{}])[0].get("value") == "心内科"
          and fu.get("time", {}).get("range") == prev["time"]["range"],
          json.dumps(fu, ensure_ascii=False)[:160])
    check("追问的置信度低于首问（不冒充重新理解了一遍）",
          fu.get("confidence", 1) < 0.6, f"confidence={fu.get('confidence')}")

    # ② 追问：改时间粒度
    fu2 = llm.generate("【用户问题】按季度看\n【对话上文】上一轮实际执行的 DSL："
                       + json.dumps(prev, ensure_ascii=False) + "\n【输出】")
    check("追问「按季度看」→ 只改粒度，时间范围与口径不变",
          fu2.get("time", {}).get("granularity") == "quarter"
          and fu2.get("time", {}).get("range") == prev["time"]["range"],
          json.dumps(fu2.get("time"), ensure_ascii=False))

    # ③ 追问：加对比
    fu3 = llm.generate("【用户问题】同比是多少\n【对话上文】上一轮实际执行的 DSL："
                       + json.dumps(prev, ensure_ascii=False) + "\n【输出】")
    check("追问「同比是多少」→ 加上对比期（否则用户会读到一个没有对比的数）",
          fu3.get("time", {}).get("compare") == "yoy")

    # ④ 追问：排除某科室（反向过滤，不能退化成"只看急诊"）
    fu4 = llm.generate("【用户问题】排除急诊\n【对话上文】上一轮实际执行的 DSL："
                       + json.dumps(prev, ensure_ascii=False) + "\n【输出】")
    f0 = (fu4.get("filters") or [{}])[0]
    check("追问「排除急诊」→ 生成 != 过滤（不是 = ）",
          f0.get("op") == "!=" and f0.get("value") == "急诊科",
          json.dumps(fu4.get("filters"), ensure_ascii=False))

    # ⑤ 追问落到 SQL：科室码被翻译成 CARD、时间范围还是 2023
    #    （关掉歧义反问：这里验的是「指代结果」，科室归属反问是另一条产品行为）
    p2 = pipeline(enable_ambiguity=False)
    r2 = p2.ask("那心内科呢", role="院长", dept_scope=["*"], context=ctx)
    sql2 = r2.sql or ""
    check("追问端到端：SQL 里出现科室码 CARD 与上文的时间范围",
          r2.status == "answered" and "CARD" in sql2 and "2023-01-01" in sql2,
          f"status={r2.status} sql={sql2[:80]}")
    r5 = p2.ask("排除急诊", role="院长", dept_scope=["*"], context=ctx)
    check("追问端到端：排除急诊 → SQL 里是 EM 的反向过滤",
          r5.status == "answered" and "EM" in (r5.sql or "") and "!=" in (r5.sql or ""),
          f"status={r5.status}")

    # ⑥ ★ 没有上文时不许猜：『那心内科呢』不是一句完整的问题
    fu6 = llm.generate("【用户问题】那心内科呢\n【输出】")
    check("无上文时『那心内科呢』→ 明确超出范围（绝不瞎猜指标）",
          fu6.get("intent") == "out_of_scope", json.dumps(fu6, ensure_ascii=False))
    r6 = pipeline().ask("那心内科呢", role="院长", dept_scope=["*"])
    check("无上文时端到端也是 out_of_scope（不是 error、不是乱出数）",
          r6.status == "out_of_scope" and not r6.sql, f"status={r6.status}")

    print("=" * 60)
    print("自测结果: " + ("全部通过" if passed else "存在失败"))
    return passed


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
