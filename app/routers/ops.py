# -*- coding: utf-8 -*-
"""F7 运营与反馈闭环。

问数是"信任生意"：前 3 个月靠反馈速度决定生死（08 §8）。
打回的问题必须能直接进 Golden 集标注队列，不能停在 Excel 里。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request

from app.core import authz, meta_db, ops_stats
from app.models import BacklogRequest, FeedbackRequest

router = APIRouter(prefix="/api/ops", tags=["F7 运营与闭环"])


@router.post("/feedback")
def feedback(req: FeedbackRequest, request: Request) -> Dict[str, Any]:
    # 反馈人取服务端解析出的身份：反馈是"谁觉得这个数不对"的证据，
    # 由调用方自报就等于谁都能替别人打回（F7.1）。
    uid = authz.principal(request, req.user_id)["user_id"]
    fid = meta_db.save_feedback(req.answer_id, uid, req.kind,
                                req.comment, req.question)
    # 「数字不对 / 口径不对」直接进回流队列
    backlogged = False
    if req.kind in ("wrong_number", "wrong_caliber") and req.question:
        meta_db.push_backlog(req.question, uid, "", reason=f"feedback:{req.kind}")
        backlogged = True
    return {"ok": True, "feedback_id": fid, "backlogged": backlogged}


# 下面四个是**运营视图**（全院谁在打回什么、哪些问题没答好）→ 限管理员。
@router.get("/feedback")
def list_feedback(request: Request, limit: int = 100) -> List[Dict[str, Any]]:
    authz.require_admin(request)
    return meta_db.list_feedback(limit=limit)


@router.get("/backlog")
def backlog(request: Request, status: str = None, limit: int = 100) -> List[Dict[str, Any]]:
    authz.require_admin(request)
    return meta_db.list_backlog(status=status, limit=limit)


@router.post("/backlog")
def push_backlog(req: BacklogRequest, request: Request) -> Dict[str, Any]:
    uid = authz.principal(request, req.user_id)["user_id"]
    bid = meta_db.push_backlog(req.question, uid, "", reason=req.reason)
    return {"ok": True, "backlog_id": bid}


@router.get("/backlog/export")
def export_backlog(request: Request, limit: int = 200) -> Dict[str, Any]:
    """导出 Golden 集标注候选（09 规范的 question 来源之一）。

    ★ 导出物是"真实用户问过的原话"，属于用户行为数据 → 限管理员。
    """
    authz.require_admin(request)
    items = meta_db.export_backlog(limit=limit)
    return {"count": len(items),
            "note": "这些是真实用户问过但没答好的问题，标注价值高于人工编造",
            "items": items}


@router.get("/rankings")
def rankings(request: Request, days: int = 30, limit: int = 10) -> Dict[str, Any]:
    """运营榜单：热度 / 未命中 / 打回 / 指标使用。

    ★ 比率不能指导动作：未命中率 12% 开不了会，
      "这周被问 37 次但答不了的 5 个问题"才能开会 —— 语义层下一批补什么看这里。
    ★ 含真实用户问法，限管理员（与流水日志同级敏感）。
    """
    authz.require_admin(request)
    return ops_stats.rankings(
        meta_db.list_audit(limit=max(500, limit * 100)),
        meta_db.list_feedback(limit=max(200, limit * 50)),
        meta_db.list_backlog(limit=max(200, limit * 50)),
        limit=limit, days=days)


@router.get("/stats")
def stats(request: Request, limit: int = 500) -> Dict[str, Any]:
    """运营指标：采纳率、打回率、反问率、未命中率、P95。（全局视图 → 管理员）"""
    authz.require_admin(request)
    logs = meta_db.list_audit(limit=limit)
    fbs = meta_db.list_feedback(limit=limit)
    total = len(logs)
    if total == 0:
        return {"total": 0, "note": "暂无数据"}

    def cnt(status: str) -> int:
        return sum(1 for r in logs if r["status"] == status)

    lat = sorted(r["latency_ms"] or 0 for r in logs)
    p95 = lat[int(len(lat) * 0.95) - 1] if lat else 0

    bad = sum(1 for f in fbs if f["kind"] != "correct")
    answered = cnt("answered")

    return {
        "total_questions": total,
        "answered": answered,
        "answer_rate": round(answered / total, 4),
        "clarify_rate": round(cnt("need_clarify") / total, 4),
        "deny_rate": round(cnt("denied") / total, 4),
        "miss_rate": round((cnt("out_of_scope") + cnt("error")) / total, 4),
        "打回率": round(bad / len(fbs), 4) if fbs else None,
        "p95_latency_ms": p95,
        "feedback_count": len(fbs),
        "backlog_pending": len(meta_db.list_backlog(status="pending")),
        # 执行层限流现状：被拒次数能直接说明"容量不够"还是"有人在扫表"
        "rate_limit": _limiter_snapshot(),
        "note": "采纳率需结合是否重问判断，此处用 answer_rate 近似；"
                "要看具体补哪些指标，请查 /api/ops/rankings",
    }


def _limiter_snapshot() -> Dict[str, Any]:
    try:
        from app.core.ratelimit import limiter
        return limiter.snapshot()
    except Exception:  # noqa: BLE001
        return {}
