# -*- coding: utf-8 -*-
"""F2 可信与口径透明 —— 本产品的护城河。

提供：口径快照查询、历史复现与差异说明、口径偏好记忆、成熟度查询。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from app.core import authz, engine_bridge, meta_db, permission
from app.core.guardrail import safe_narrative
from app.models import PrefRequest

router = APIRouter(prefix="/api/trust", tags=["F2 可信与口径透明"])


def _own(answer_id: str, user_id: str) -> Dict[str, Any]:
    """取快照并校验归属（12 §6.3：快照限本人，信息科可查全部）。

    越权一律按 404 处理：返回 403 等于告诉对方「这条快照存在」。
    `user_id` 必须是**调用方**（由 authz 解析），不是请求参数。
    """
    s = meta_db.get_snapshot(answer_id)
    if not s:
        raise HTTPException(status_code=404, detail="快照不存在")
    user = permission.resolve_user(user_id)
    if s.get("user_id") and s["user_id"] != user_id and not permission.is_admin(user):
        raise HTTPException(status_code=404, detail="快照不存在")
    return s


@router.get("/snapshots")
def snapshots(request: Request, user_id: str = "guest", limit: int = 50) -> List[Dict[str, Any]]:
    """我的快照（客户端左栏「我的快照」）。信息科看到全部。"""
    user = authz.principal(request, user_id)
    return meta_db.list_snapshots(
        limit=limit, user_id=None if permission.is_admin(user) else user["user_id"])


@router.get("/snapshot/{answer_id}")
def snapshot(answer_id: str, request: Request, user_id: str = "guest") -> Dict[str, Any]:
    return _own(answer_id, authz.principal(request, user_id)["user_id"])


@router.post("/replay/{answer_id}")
def replay(answer_id: str, request: Request, user_id: str = "guest") -> Dict[str, Any]:
    """用当时的口径 + 当时的 SQL 重跑，与快照比对。

    差异说明只陈述事实（口径变了 / 数据回刷了 / 指标定义改了），
    **不做责任判断**（10 IN-06）。
    """
    s = _own(answer_id, authz.principal(request, user_id)["user_id"])
    if not s.get("sql_text"):
        raise HTTPException(status_code=400, detail="该快照无 SQL，无法复现")

    try:
        # ★ 必须走 engine_bridge.executor：它按当前数据源路由（ADS / HIS）。
        #   此前写死 `his.query`，切到 A 路线（ADS 宽表）后复现必然失败 ——
        #   而"复现不了"在用户眼里等于"这个数字不可信"。
        rows = engine_bridge.executor(s["sql_text"])
    except Exception as e:
        return {"replayed": False, "error": f"重跑失败：{e}"}

    from app.core.caliber import _hash
    h = _hash(rows)

    old_rows = json.loads(s["result_json"]) if s.get("result_json") else []
    same = (h == s.get("result_hash"))

    reasons: List[str] = []
    if not same:
        if len(rows) != len(old_rows):
            reasons.append(f"结果行数由 {len(old_rows)} 变为 {len(rows)}")
        reasons.append("可能是数据回刷（源库重算）或口径参数变化")

    return {
        "replayed": True,
        "answer_id": answer_id,
        "identical": same,
        "old_hash": s.get("result_hash"),
        "new_hash": h,
        "old_row_count": len(old_rows),
        "new_row_count": len(rows),
        "rows": rows[:50],
        "snapshot": {"dsl": s.get("dsl_json"), "caliber": s.get("caliber_json"),
                     "data_as_of": s.get("data_as_of"), "maturity": s.get("maturity"),
                     "metric_ver": s.get("metric_ver")},
        "difference_notes": reasons,      # 只陈述事实
    }


# ── 口径偏好记忆（F2.2）──
# 偏好是"我的习惯"，读写都得是本人（管理员可维护）。虽然敏感度低，
# 但"能改别人的默认口径"本身就是一种越权：改完受影响的却不在现场。
@router.get("/prefs/{user_id}")
def list_prefs(user_id: str, request: Request) -> Dict[str, Any]:
    """按**指标码**分组的偏好：{scope: {key: value}}。

    ★ 不复用扁平的 `/pref/{user_id}`：那个会把不同指标的同名 key 合并，
      拿它渲染界面就会让"某指标记住的口径"看起来对所有指标都生效
      （app/core/meta_db.py::get_prefs_all 已记录过这个坑）。
    """
    authz.require_self_or_admin(request, user_id)
    return {"user_id": user_id, "prefs": meta_db.get_prefs_all(user_id)}


@router.get("/pref/{user_id}")
def get_pref(user_id: str, request: Request, scope: Optional[str] = None) -> Dict[str, str]:
    authz.require_self_or_admin(request, user_id)
    return meta_db.get_prefs(user_id, scope)


@router.post("/pref")
def set_pref(req: PrefRequest, request: Request) -> Dict[str, Any]:
    """偏好只影响 default，不影响 supported —— 用户无权突破语义层约束。"""
    authz.require_self_or_admin(request, req.user_id)
    meta_db.set_pref(req.user_id, req.scope, req.key, req.value)
    return {"ok": True, **req.dict()}


@router.delete("/pref/{user_id}/{scope}/{key}")
def del_pref(user_id: str, scope: str, key: str, request: Request) -> Dict[str, Any]:
    authz.require_self_or_admin(request, user_id)
    meta_db.clear_pref(user_id, scope, key)
    return {"ok": True}


# ── 数字护城河（F2.6）──
@router.post("/narrative/check")
def check_narrative(payload: Dict[str, Any]) -> Dict[str, Any]:
    """校验一段解读文本：数字必须来自授权集合，且不得有因果断言。"""
    text = payload.get("text", "")
    rows = payload.get("rows") or []
    allow_causal = bool(payload.get("allow_causal", False))
    return safe_narrative(text, rows, allow_causal=allow_causal)


# ── 数据质量分 Q（IN-03 最小版）──
@router.get("/quality")
def quality_all(request: Request, user_id: str = "guest") -> Dict[str, Any]:
    """全院指标质量分（低分在前，便于先修最差的）。

    ★ 分数只来自**观测事实**：源新鲜度、近 7 天失败率/空结果率、本体治理完备性。
      它不是模型打分，也不做"综合印象评估"——每条扣分都必须能指到一条证据。
    """
    authz.principal(request, user_id)
    from app.core import quality
    items = quality.score_all()
    low = [i for i in items if i["level"] == "low"]
    return {
        "count": len(items),
        "low_count": len(low),
        "items": items,
        "levels": {"high": "≥0.8 可作为决策依据", "medium": "0.5~0.8 可用但需留意",
                   "low": "<0.5 不建议直接引用，请先修数据或补本体"},
    }


@router.get("/quality/{metric}")
def quality_one(metric: str, request: Request, user_id: str = "guest") -> Dict[str, Any]:
    """单个指标的质量分（问数结果卡上的 Q 徽标数据源）。"""
    authz.principal(request, user_id)
    from app.core import quality
    return quality.score_metric(metric)


@router.get("/maturity")
def maturity() -> Dict[str, Any]:
    """各数据域的成熟度现状（含 source_stale）。"""
    states = meta_db.list_source_state()
    stale = [s["domain"] for s in states if s.get("freshness") == "stale"]
    return {
        "domains": states,
        "stale_domains": stale,
        "legend": {
            "settled": "数据已结算落定",
            "provisional": "未结算，数值可能变化",
            "restated": "已修正，可查原值",
            "source_stale": "源数据已停止更新，不可作为现状使用",
        },
    }
