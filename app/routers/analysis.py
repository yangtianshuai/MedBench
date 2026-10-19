# -*- coding: utf-8 -*-
"""F3 分析能力 —— 下钻 / 对比 / 排名 / 趋势 / 归因。

五项都走**同一条受限生成链路**（语义层 DSL + RLS + 小样本 + 口径写回），
不另开自由度。归因（IN-04）在此之上只多一层数学分解，不自己拼 SQL。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from app.core import (attribution_service, authz, chat, engine_bridge, meta_db,
                      permission)
from app.core.attribution_service import AttributionError, DecompositionError

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT / "engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "engine"))

router = APIRouter(prefix="/api/analysis", tags=["F3 分析能力"])

# 分析动作的人话名（写回会话时告诉用户"这一轮是什么动作"）
_ACTION_LABEL = {"compare": "对比", "rank": "排名", "trend": "趋势", "drill": "下钻"}


def _ask(question: str, user_id: str, dsl: Optional[Dict[str, Any]] = None,
         session_id: Optional[str] = None, action: Optional[str] = None):
    """分析动作 = 同一条受限生成链路 + 一份**继承来的有效 DSL**。

    ★ 结果同样写回会话（F1.9）：分析是对话的一部分，不是另一条平行入口。
      否则用户在对话流里会看到"下一轮的上文"退回到分析之前的口径。
    """
    # ★ `user_id` 必须已经由路由层用 `authz.principal()` 解析过：
    #   本函数只负责"按已确定的主体去查"，不再接受调用方自报身份。
    user = permission.resolve_user(user_id)
    out = engine_bridge.ask(question=question, user_id=user["user_id"], role=user["role"],
                            dept_scope=user["dept_scope"],
                            campus_scope=user.get("campus_scope"), inject_dsl=dsl)
    out["user"] = {"user_id": user["user_id"], "role": user["role"], "name": user["name"],
                   "can_view_sql": user.get("can_view_sql", False),
                   "can_export": user.get("can_export", False)}
    if not user.get("can_view_sql", False):
        out["sql"] = None
    if action:
        out["analysis"] = _ACTION_LABEL.get(action, action)
    if session_id:
        sid = chat.ensure_session(user_id, session_id, title=question)
        out["session_id"] = sid
        out["message_id"] = chat.log_turn(sid, user_id, question, out=out)
    return out


def _sid(payload: Dict[str, Any]) -> Optional[str]:
    return payload.get("session_id")


# 四个分析接口统一：**先由 authz 定身份**，再把已确定的 uid 交给 _ask。
# 生产模式下 body 里的 user_id 会被忽略（只认网关注入的可信头）。
@router.post("/compare")
def compare(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """同比 / 环比 / 科室间对比。"""
    uid = authz.principal(request, payload.get("user_id"))["user_id"]
    return _ask(payload.get("question", ""), uid,
                payload.get("dsl"), _sid(payload), "compare")


@router.post("/rank")
def rank(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    uid = authz.principal(request, payload.get("user_id"))["user_id"]
    return _ask(payload.get("question", ""), uid,
                payload.get("dsl"), _sid(payload), "rank")


@router.post("/trend")
def trend(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    uid = authz.principal(request, payload.get("user_id"))["user_id"]
    return _ask(payload.get("question", ""), uid,
                payload.get("dsl"), _sid(payload), "trend")


@router.post("/drill")
def drill(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """下钻：在上一次回答的口径上加一个维度重问（继承上文口径，不重新解释）。

    例：上一次按科室看 → 下钻到「科室 × 医疗组」。
    """
    answer_id = payload.get("answer_id")
    dim = payload.get("dimension")
    if not answer_id or not dim:
        raise HTTPException(status_code=400, detail="需要 answer_id 与 dimension")

    snap = meta_db.get_snapshot(answer_id)
    if not snap or not snap.get("dsl_json"):
        raise HTTPException(status_code=404, detail="快照不存在或无 DSL")

    # ★ 下钻读的是**别人的快照**里的 DSL 与口径。没有这一步，
    #   拿到一个 answer_id 就能下钻出任何人授权范围内的数据（12 §6.3）。
    user = authz.principal(request, payload.get("user_id"))
    if snap.get("user_id") and snap["user_id"] != user["user_id"] \
            and not permission.is_admin(user):
        raise HTTPException(status_code=404, detail="快照不存在")

    dsl = json.loads(snap["dsl_json"])
    dims = list(dsl.get("dimensions") or [])
    if dim not in dims:
        dims.append(dim)
    dsl["dimensions"] = dims

    return _ask(snap.get("question") or "", user["user_id"], dsl, _sid(payload), "drill")


@router.post("/attribution")
def attribution(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """结构-价格两因素分解（IN-04）。

    「次均费用涨了 8%，是病例变难了还是单价涨了？」——两者的管理动作相反，
    普通 BI 只能回答「哪个科室涨最多」。

    ★ 取数走主链路（RLS / 成熟度 / 小样本 / 口径写回全部生效），
      本接口只新增**数学层**能力，不开旁路。
    ★ 恒等式不成立时引擎会拒绝出结论，这里原样转达，不做"差不多就行"的兜底。
    """
    # ★ 身份必须由服务端解析：归因读的是科室级明细汇总，不能接受调用方自报身份
    principal = authz.principal(request, payload.get("user_id"))
    user = permission.resolve_user(principal["user_id"])

    dimension = payload.get("dimension") or "dept"
    # DRG 分组维度门禁：分组器版本跨期不一致时分解无意义（仅对 DRG 维度生效）
    if dimension == "drg" and not payload.get("force"):
        st = meta_db.get_source_state("DRG")
        if not st or st.get("freshness") == "stale":
            return {
                "applied": False,
                "blocked": True,
                "reason": "DRG 源数据已停更（freshness=stale），跨期分组器版本无法保证一致，"
                          "分解结果不可信。按 IN-04 门禁，拒绝输出分解。",
                "source_state": st,
            }

    try:
        out = attribution_service.run(
            metric=payload.get("metric") or "ip_avg_fee",
            dimension=dimension,
            period=payload.get("period") or "mom",
            role=user["role"],
            dept_scope=user["dept_scope"],
            campus_scope=user.get("campus_scope"),
            date_basis=payload.get("date_basis"),
            dept_basis=payload.get("dept_basis"),
            min_group_size=payload.get("min_group_size"),
        )
    except AttributionError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except DecompositionError as e:
        # 引擎拒绝出结论（恒等式不成立 / 无共同组 / 数据不足）→ 如实返回，不降级
        return {"applied": False, "blocked": True, "reason": str(e)}

    if not user.get("can_view_sql", False):
        out["sql"] = None
    out["user"] = {"user_id": user["user_id"], "role": user["role"],
                   "name": user["name"], "can_view_sql": user.get("can_view_sql", False)}
    return out
