# -*- coding: utf-8 -*-
"""F1.9 会话与历史 —— 客户端「以对话为核心」的接口层。

只管会话（建/列/读/改名/软删）与消息回放；
真正的问数仍在 `/api/ask`（它会把这一轮追加进会话，见 `app/routers/ask.py`）。

**归属是这一层的唯一硬约束**（12 §6.3：快照/历史限本人）：
所有读接口都要求 `user_id`，拿不到自己的会话一律 404 ——
不返 403、不说"这是别人的会话"，避免泄露"该 ID 存在"。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from app.core import authz, chat, meta_db
from app.models import SessionIn

router = APIRouter(prefix="/api/chat", tags=["F1 会话与历史"])


@router.post("/sessions")
def new_session(req: SessionIn, request: Request) -> Dict[str, Any]:
    """新建会话（也可以什么都不传，直接发 `/api/ask` 让它自动建）。"""
    uid = authz.principal(request, req.user_id)["user_id"]
    sid = meta_db.create_session(uid, title=req.title or "")
    s = meta_db.get_session(sid, user_id=uid)
    return {"session_id": sid, "session": s}


@router.get("/sessions")
def my_sessions(request: Request, user_id: str = "guest", limit: int = 50) -> Dict[str, Any]:
    """我的会话列表（左栏"历史"）。标题取自首问，不随追问改写。"""
    uid = authz.principal(request, user_id)["user_id"]
    return {"user_id": uid, "sessions": meta_db.list_sessions(uid, limit=limit)}


@router.get("/sessions/{session_id}")
def thread(session_id: str, request: Request, user_id: str = "guest") -> Dict[str, Any]:
    """会话全文（含每轮 assistant 的渲染载荷），供客户端铺成对话流。"""
    uid = authz.principal(request, user_id)["user_id"]
    t = chat.load_thread(session_id, uid)
    if not t:
        raise HTTPException(status_code=404, detail="会话不存在")
    return t


@router.patch("/sessions/{session_id}")
def rename(session_id: str, req: SessionIn, request: Request) -> Dict[str, Any]:
    uid = authz.principal(request, req.user_id)["user_id"]
    if not meta_db.rename_session(session_id, uid, req.title or ""):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "session_id": session_id, "title": req.title}


@router.delete("/sessions/{session_id}")
def remove(session_id: str, request: Request, user_id: str = "guest") -> Dict[str, Any]:
    """软删除。

    真删除会让"谁在什么时候问过什么"出现空洞（12 §6.2 审计不可篡改），
    所以这里只把它从列表里隐去，落库记录仍在。
    """
    uid = authz.principal(request, user_id)["user_id"]
    if not meta_db.delete_session(session_id, uid):
        raise HTTPException(status_code=404, detail="会话不存在")
    return {"ok": True, "session_id": session_id, "soft_deleted": True}


@router.get("/examples")
def examples() -> List[Dict[str, str]]:
    """按会话阶段给的"接下来能问什么"（F1.7 冷启动 + 追问建议）。

    与 `/api/ask/examples` 的区别：那份是"第一次来能问什么"，
    这份是"已经问过一轮之后，还能接着问什么"。
    """
    return [
        {"question": "那心内科呢", "tag": "追问"},
        {"question": "按季度看", "tag": "改粒度"},
        {"question": "同比是多少", "tag": "对比"},
        {"question": "和去年比怎么样", "tag": "对比"},
    ]
