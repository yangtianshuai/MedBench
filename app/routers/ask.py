# -*- coding: utf-8 -*-
"""F1 对话问数 —— 核心交互。

链路：患者级围栏 → 越权预检 → 召回 → LLM → 校验 → 权限 → 反问 → SQL → 执行
     → 小样本上卷 → 可信标记 → 审计留痕 → 快照 → 写入会话（F1.9）

多轮（F1.2 指代消解）：同一 `session_id` 内，把上一轮**实际执行的有效 DSL**
当作上文交给引擎（`context`）。上文只在本会话内传递，绝不跨会话、不跨用户。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.core import authz, chat, engine_bridge, meta_db, permission, rollup
from app.models import AskRequest

router = APIRouter(prefix="/api/ask", tags=["F1 对话问数"])


@router.post("")
def ask(req: AskRequest, request: Request) -> Dict[str, Any]:
    # 身份由服务端解析：demo 模式用参数（可切换，不是安全边界），
    # gateway/sso 模式只认网关注入的可信头 —— 后者会**忽略** body 里的 user_id，
    # 否则"网关鉴权"能被一个 JSON 字段绕过（docs/12 §3.4）。
    user = authz.principal(request, req.user_id)
    uid = user["user_id"]

    # ★ 会话先落地：没有会话就没有"上文"，指代消解无从谈起（F1.9）。
    #   传入的 session_id 不属于本人时会**新建**一个而不是复用（见 app/core/chat.py）。
    sid = chat.ensure_session(uid, req.session_id, title=req.question)

    # 上一轮的有效 DSL（拿不到就返回 None，引擎会当"没有上文"）
    context = meta_db.last_turn_context(sid) if req.follow_up else None

    try:
        out = engine_bridge.ask(
            question=req.question,
            user_id=uid,
            role=user["role"],
            dept_scope=user["dept_scope"],
            # 院区范围（F6.3）：None = 未启用院区围栏（账号没配这一项）
            campus_scope=user.get("campus_scope"),
            resolved=req.resolved,
            inject_dsl=req.dsl,
            context=context,
        )
    except RuntimeError as e:
        # 管线都没起来（模型未配置/源库不可达）：这一轮也要留在会话里，
        # 否则用户回看历史会以为"我没问过这句"。
        chat.log_turn(sid, uid, req.question, status="error")
        raise HTTPException(status_code=503, detail=str(e))

    # 小样本保护（F2.7）：有 cnt 列时按样本量上卷
    rows = out.get("rows") or []
    if rows and "cnt" in (rows[0] or {}):
        dim = _first_dim(rows[0])
        if dim:
            rr = rollup.rollup_small_cells(rows, dim=dim, count_key="cnt")
            if rr["applied"]:
                out["rows"] = rr["rows"]
                out["warnings"] = (out.get("warnings") or []) + [rr["note"]]
                out["rollup"] = {"applied": True, "rolled": rr["rolled"]}

    out["user"] = {"user_id": user["user_id"], "role": user["role"],
                   "name": user["name"], "can_view_sql": user.get("can_view_sql", False),
                   "can_export": user.get("can_export", False)}

    # 列级权限：非授权角色不返回 SQL
    if not user.get("can_view_sql", False):
        out["sql"] = None

    # 答不了的问题自动进回流队列（F7.2）
    if out["status"] in ("out_of_scope", "error"):
        meta_db.push_backlog(req.question, uid, user["role"],
                             reason=f"status={out['status']}")
        out["backlogged"] = True

    # 会话回执：前端据此把这一轮挂到对话流上（不依赖"猜"）
    out["session_id"] = sid
    out["follow_up"] = bool(req.follow_up)
    out["context_used"] = bool(context)
    out["message_id"] = chat.log_turn(sid, uid, req.question, out=out)
    return out


@router.post("/clarify")
def clarify(req: AskRequest, request: Request) -> Dict[str, Any]:
    """携带澄清结果再问一次（F1.3）。"""
    if not req.resolved:
        raise HTTPException(status_code=400, detail="resolved 不能为空")
    return ask(req, request)


@router.get("/examples")
def examples() -> List[Dict[str, str]]:
    """冷启动引导（F1.7）：给几个能答对的示例，别让用户第一句就撞墙。"""
    return [
        {"question": "上个月全院出院人次是多少", "tag": "住院"},
        {"question": "各科室平均住院日排名", "tag": "排名"},
        {"question": "心内科住院次均费用同比", "tag": "对比"},
        {"question": "平均住院日是怎么定义的", "tag": "问口径"},
    ]


def _first_dim(row: Dict[str, Any]) -> str:
    for k in ("dept_name", "dept_code", "dept", "科室"):
        if k in row:
            return k
    keys = [k for k in row.keys() if k != "cnt"]
    return keys[0] if keys else ""
