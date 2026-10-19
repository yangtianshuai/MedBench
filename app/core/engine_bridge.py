# -*- coding: utf-8 -*-
"""问数引擎桥接层（L5）。

复用 engine/pipeline.py 的既有实现（意图路由 / 反问 / 受限生成 / 权限门禁），
只把「执行器」换成真实的 HIS 只读数据源。
"""

from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_DIR = ROOT / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import pipeline as eng  # noqa: E402  （engine/pipeline.py）
from app.config import settings  # noqa: E402
from app.core import meta_db  # noqa: E402
from app.core.his import his, ReadOnlyViolation  # noqa: E402
from app.core.registry import load_ambiguity, load_registry  # noqa: E402

_pipeline = None


def _build_llm():
    """构造 LLM 适配器。

    优先级：
      1. 配置了 ASK_LLM_BASE_URL/KEY → HTTPLLM（须指向院内私有化服务）
      2. ASK_LLM_MOCK=1 → MockOracle（仅自测/冒烟，禁止用于正式环境）
      3. 都没有 → None，ask 时返回明确错误（绝不静默降级为编造）
    """
    if settings.llm_base_url and settings.llm_api_key:
        return eng.HTTPLLM(
            model=settings.llm_model or None,
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key,
            timeout=settings.llm_timeout,
        )
    import os
    if os.getenv("ASK_LLM_MOCK") == "1":
        # 降级路径：用语义层别名做规则匹配，让没有模型的环境也能演示完整链路。
        # 不用 MockOracleLLM({}) —— 它返回空 DSL，会让界面上每个问题都失败，
        # 看起来像系统坏了，而实际只是没装模型（两种情况必须能区分）。
        from app.core.rule_llm import RuleBasedLLM
        return RuleBasedLLM(load_registry())
    return None


def executor(sql: str) -> List[Dict[str, Any]]:
    """管线执行器：按 data_source 把 SQL 交给 HIS（B 路线）或 ADS（A 路线）。

    两条路线共用同一份语义层 —— 切过去若要改指标定义，就是设计的失败。
    """
    # ★ 所有 SQL 都必须穿过限流：这是唯一能真正保护 HIS 的位置
    #   （HTTP 层只能挡人，挡不住一个跑 10 分钟的查询）
    from app.core.ratelimit import limiter
    with limiter.guard():
        if settings.data_source == "ads":
            from app.core import ads
            return ads.query(sql, limit=settings.max_rows)
        return his.query(sql, max_rows=settings.max_rows, timeout=settings.query_timeout)


def get_pipeline(force: bool = False):
    global _pipeline
    if _pipeline is None or force:
        _pipeline = eng.AskPipeline(
            registry=load_registry(),
            ambiguity=load_ambiguity(),
            llm=_build_llm(),
            executor=executor,
            today=None,
            enable_fewshot=settings.fewshot_enabled,
            examples_path=settings.examples_path,
            dialect=settings.dialect,
        )
    return _pipeline


def answer_id() -> str:
    return uuid.uuid4().hex[:16]


# ────────────────────────────────────────────────────────────
# 维度可用性探测（F3 下钻 / 排名的前置）
# ────────────────────────────────────────────────────────────
_probe_pipe = None
_probe_cache: Dict[str, Any] = {}
_PROBE_TTL = 300          # 秒：口径/物化都可能变，但不必每次都验


def _get_probe_pipeline():
    """无执行器、不做反问的管线：只用来造 SQL。"""
    global _probe_pipe
    if _probe_pipe is None:
        _probe_pipe = eng.AskPipeline(registry=load_registry(), ambiguity=load_ambiguity(),
                                      llm=None, executor=None, enable_ambiguity=False,
                                      dialect=settings.dialect)
    return _probe_pipe


def probe_dimension(metric_code: str, dim_code: str, role: str,
                    dept_scope: Optional[List[str]] = None) -> Dict[str, Any]:
    """探测「指标 × 维度」在**当前数据源**上到底能不能用。

    为什么必须真探一次，而不是照着语义层声明报"可用"：
      声明的维度不一定物化到了当前数据源。实测 ADS 宽表里就没有
      pay_type_code / discharge_type_code —— 按这些维度下钻只会拿到
      "no such column" 这样的数据库错误。**界面不该给出一个点了就报错的按钮。**

    做法：用**真正的 SQL 生成器**造一条只含该维度的查询，再让数据库用
    `SELECT * FROM (...) WHERE 1=0` 做一次纯元数据校验（不扫数据、不返回行）。
    列名由生成器决定（如 dept 要按当前口径取 discharge_dept_code / admit_dept_code），
    所以只能这样验：自己猜元数据一定会错。

    返回 {"usable": bool, "reason": str|None, "checked": "pipeline"|"db"}
      checked=pipeline → 生成/权限阶段就被拒（如受限维度），reason 即引擎原话；
      checked=db       → 生成 OK，但物理层没有这个列。
    """
    scope = tuple(dept_scope or [])
    key = f"{metric_code}|{dim_code}|{role}|{','.join(sorted(scope))}"
    hit = _probe_cache.get(key)
    if hit and time.time() - hit[0] < _PROBE_TTL:
        return hit[1]

    result: Dict[str, Any] = {"usable": False, "reason": None, "checked": None}
    dsl = {"intent": "query_metric", "metrics": [metric_code], "dimensions": [dim_code],
           "time": {"granularity": "year", "range": {"type": "last_month"}}}
    try:
        res = _get_probe_pipeline().ask(f"[dimension-probe] {metric_code} × {dim_code}",
                                        role=role, dept_scope=scope, inject_dsl=dsl)
    except Exception as e:                      # 生成器自己抛错也不能把界面搞崩
        result.update(reason=f"维度探测失败：{e}", checked="pipeline")
        _probe_cache[key] = (time.time(), result)
        return result

    if res.status != "answered" or not res.sql:
        result.update(reason=(res.answer or "该维度当前不可用").splitlines()[0][:200],
                      checked="pipeline")
        _probe_cache[key] = (time.time(), result)
        return result

    try:
        executor(f"SELECT * FROM (\n{res.sql}\n) WHERE 1=0")
        result.update(usable=True, checked="db")
    except Exception as e:
        msg = str(e).splitlines()[0][:160]
        result.update(reason=f"当前数据源没有物化这个维度（{msg}）", checked="db")
    _probe_cache[key] = (time.time(), result)
    return result


def ask(question: str, user_id: str, role: str, dept_scope: Optional[List[str]] = None,
        campus_scope: Optional[List[str]] = None,
        resolved: Optional[Dict[str, str]] = None,
        inject_dsl: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        record: bool = True) -> Dict[str, Any]:
    """主入口：问一句，返回结构化结果（含可信信息）。

    campus_scope：院区数据范围（F6.3）。
      `None` = 未启用院区围栏（角色/用户都没配）→ 行为与从前完全一致；
      `[]`   = 显式无授权 → 拒绝；`["*"]` = 全院区。
      ★ 不要把 None 当成 [] 处理：前者是"这项还没启用"，后者是"明确不给权限"。

    context = 同一会话里的上一轮（{"question","dsl"}，F1.2 指代消解）。
    由上层从 `meta_db.last_turn_context()` 取，**调用方负责确认是同一会话**。
    """
    from app.core.caliber import build_trust

    started = time.time()
    pipe = get_pipeline()
    if pipe.llm is None:
        raise RuntimeError(
            "未配置模型服务。医院场景必须指向院内网私有化部署的模型"
            "（设置 ASK_LLM_BASE_URL / ASK_LLM_API_KEY），"
            "或设置 ASK_LLM_MOCK=1 仅用于本地冒烟。")

    # ★ 禁止 `or ["*"]` 兜底（v0.9 修正）：空范围 = 无授权，必须原样交给管线拒绝。
    #   此处曾写成 `tuple(dept_scope or ["*"])`，使 guest 拿到全院通配符，
    #   越权预检 / 科室检查 / SQL 层 RLS 三处同时失效（详见 docs/12 §1.2 S2）。
    scope = tuple(dept_scope or [])
    # ★ 院区范围同样不做 `or ["*"]` 兜底；None 原样传下去表示"未启用"。
    campus = None if campus_scope is None else tuple(campus_scope)
    if inject_dsl is not None and "time" not in inject_dsl:
        # 兼容 Golden 标注的扁平格式（含顶层 time_range）
        inject_dsl = eng.dsl_from_golden(inject_dsl)
    res = pipe.ask(question, role=role, dept_scope=scope, campus_scope=campus,
                   resolved=resolved, inject_dsl=inject_dsl, context=context)
    latency_ms = int((time.time() - started) * 1000)

    aid = answer_id()
    metric_code = None
    # ★ 有效 DSL = 口径写回（docs/13 §7 C9 的步骤 4.5）之后的 DSL，**这才是真正执行的那个**。
    #   快照 / 下钻 / 前端回显一律用它；模型原始输出（raw）只进审计与排查。
    #   混用会出人命：用户在结果卡上选「结算日期」再下钻，快照里却是写回前的 DSL，
    #   下钻结果会悄悄退回默认口径（同 C9 那类静默换口径的事故）。
    effective_dsl = res.dsl.to_dict() if res.dsl is not None else res.raw_dsl
    if effective_dsl:
        ms = effective_dsl.get("metrics") or []
        metric_code = ms[0] if ms else None

    trust = build_trust(res, metric_code)
    # 质量分 Q（IN-03 最小版）：只由已观测事实（源新鲜度 / 近 7 天失败率 /
    # 本体治理完备性）算出，理由逐条附在 trust.quality.reasons。
    # 取数失败绝不能影响出数 —— 这里降级为"未评估"，而不是让整个回答 500。
    try:
        from app.core import quality
        trust["quality"] = quality.score_metric(metric_code, load_registry())
    except Exception:  # noqa: BLE001
        trust["quality"] = {"metric": metric_code, "score": None,
                            "level": "unknown", "reasons": ["质量分计算失败（不影响出数）"]}

    payload = {
        "answer_id": aid,
        "status": res.status,
        "question": res.question,
        "answer": res.answer,
        "metric_code": metric_code,
        "intent": (res.raw_dsl or {}).get("intent"),
        "dsl": effective_dsl,          # 实际执行的口径（前端回显用这个）
        "dsl_raw": res.raw_dsl,        # 模型原始输出（排查模型用，不等于实际执行口径）
        "sql": res.sql,
        "caliber": res.caliber,
        "rows": res.rows,
        # 结构化反问项（docs/13 §7 C1）：卡片式反问的数据来源。
        # 此前只有 answer 里的一段文本，前端做不出「选项按钮」。
        "clarify": res.clarify,
        "warnings": res.warnings,
        "trace": res.trace,
        "trust": trust,
        "latency_ms": latency_ms,
        "registry_version": load_registry().get("version"),
    }

    # record=False：内层取数（如归因要跑基期/报告期两条）不单独留痕，
    # 由外层动作统一留一条，否则一次归因会在审计里长成两三条"用户提问"。
    if record and settings.audit_enabled:
        # 审计留模型原始输出（可用于排查模型/口径识别问题），
        # 实际执行的口径在快照的 dsl_json 里，两者分工不同。
        meta_db.log_audit(
            user_id=user_id, role=role, question=question, status=res.status,
            intent=payload["intent"], metric_code=metric_code, dsl=res.raw_dsl,
            sql=res.sql, row_count=len(res.rows or []),
            result_hash=trust.get("result_hash"), latency_ms=latency_ms, answer_id=aid)

    # 有结果的回答落快照（F2.5），供复现与差异说明
    if record and res.rows is not None and res.status == "answered":
        meta_db.save_snapshot({
            "answer_id": aid, "ts": time.time(), "user_id": user_id,
            "question": question, "metric_code": metric_code,
            "metric_ver": load_registry().get("version"),
            "caliber_json": _dump(res.caliber), "dsl_json": _dump(effective_dsl),
            "sql_text": res.sql, "result_json": _dump(res.rows),
            "result_hash": trust.get("result_hash"),
            "data_as_of": trust.get("data_as_of"), "maturity": trust.get("maturity"),
        })
    return payload


def _dump(obj: Any) -> Optional[str]:
    import json
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return None
