# -*- coding: utf-8 -*-
"""F5 语义层与指标资产后台。

指标口径是**人工资产**：注册要填责任人，变更要留痕，争议先下线（08 R1）。
本模块不做任何口径推导 —— 那属于 LLM 的禁区（ADR-03）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from app.core import authz, engine_bridge, meta_db, permission
from app.core import registry as reg
from app.models import MetricIn, RetireRequest

router = APIRouter(prefix="/api/semantic", tags=["F5 语义层后台"])


@router.get("/metrics")
def metrics(request: Request, user_id: str = "guest", domain: str = None,
            keyword: str = None) -> Dict[str, Any]:
    user = authz.principal(request, user_id)
    items = permission.filter_metrics(user, reg.list_metrics(domain=domain, keyword=keyword))
    return {"registry_version": reg.registry_version(),
            "count": len(items),
            "metrics": items}


@router.get("/domains")
def domains() -> List[str]:
    return reg.metric_domains()


@router.get("/metrics/{code}")
def metric(code: str) -> Dict[str, Any]:
    m = reg.get_metric(code)
    if not m:
        raise HTTPException(status_code=404, detail="指标不存在")
    versions = meta_db.list_metric_versions(code)
    # 可切换口径一并给出（来自 measure 的 supported 列表）：
    # 客户端口径抽屉（F2.2）与后台指标编辑器（F5.1）都只认这一处，避免两处各推一遍。
    return {"metric": m, "versions": versions, **reg.caliber_options(code)}


@router.get("/dimensions")
def dimensions(metric: str, request: Request, user_id: str = "guest") -> Dict[str, Any]:
    """某指标**当前用户真正能用的分组/下钻维度**（F3 下钻与排名用）。

    「可用」= 语义层声明 ∧ 权限允许 ∧ **当前数据源确实物化了**，三者缺一不可：

    - 权限：维度授权独立于指标授权（`restricted_dimension_roles`），
      「医师」涉及个人绩效，能看受限指标也不等于能下钻到个人；
    - 物化：实测 ADS 宽表里没有 `pay_type_code` / `discharge_type_code`，
      按语义层声明报"可用"就是在给用户一个点了必报错的按钮。

    判断不在这里重写一遍：直接拿引擎跑一次探测（`engine_bridge.probe_dimension`），
    把引擎/数据库的原话作为不可用原因回给界面 —— 一份逻辑，一个口径。
    """
    m = reg.get_metric(metric)
    if not m:
        raise HTTPException(status_code=404, detail="指标不存在")
    reg_data = reg.load_registry()
    user = authz.principal(request, user_id)
    dim_meta = {d.get("code"): d for d in (reg_data.get("dimensions") or [])}
    out: List[Dict[str, Any]] = []
    for code in (m.get("dimensions") or []):
        meta = dim_meta.get(code) or {}
        probe = engine_bridge.probe_dimension(metric, code, user["role"], user["dept_scope"])
        out.append({"code": code, "name": meta.get("name", code),
                    "type": meta.get("type"),
                    "sensitivity": meta.get("sensitivity", "public"),
                    "usable": probe["usable"],
                    "reason": probe["reason"],
                    "checked_by": probe["checked"]})
    return {"metric": metric, "metric_name": m.get("name"),
            "role": user["role"], "dimensions": out}


@router.post("/metrics")
def upsert(m: MetricIn, request: Request) -> Dict[str, Any]:
    """注册/更新指标定义 —— **改的是全院的指标资产**，故限管理员（12 §3.4）。

    此前"谁能改指标"只由前端藏按钮决定：一个 curl 就能改掉全院的敏感级与责任人。
    """
    p = authz.require_admin(request, m.user_id)
    if not m.owner:
        raise HTTPException(status_code=400,
                            detail="指标必须指定 owner（每个指标有且仅有一个说了算的人）")
    # 操作人取自**服务端解析出的身份**，不采信请求体里的 operator：
    # 版本留痕写谁改的，这一栏不能由调用方自己填。
    # exclude 里的三个字段都不是指标内容（user_id/operator/change_note），写库前必须排除，
    # 否则它们会被当成指标字段落进语义层。
    try:
        return reg.upsert_metric(
            m.dict(exclude={"user_id", "operator", "change_note"}),
            operator=p["user_id"], change_note=m.change_note or "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/metrics/{code}/retire")
def retire(code: str, req: RetireRequest, request: Request) -> Dict[str, Any]:
    p = authz.require_admin(request, req.user_id)
    try:
        return reg.retire_metric(code, p["user_id"], req.reason)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/versions")
def versions(request: Request, code: str = None, limit: int = 50) -> List[Dict[str, Any]]:
    """指标版本留痕 —— 治理信息（谁改了什么），限管理端。"""
    authz.require_admin(request)
    return meta_db.list_metric_versions(code, limit=limit)


@router.get("/reconcile")
def reconcile(request: Request, domain: str = "住院") -> Dict[str, Any]:
    """口径对账（F5.7）：拿 HIS 自带报表层做黄金基准。

    用法：把本院 HIS 的 REPORT.RPT_DEPT_DAY（科室×日×指标项）与我们按
    PAT_VISIT 重算的结果逐项比对。对得上 → 口径理解一致；
    对不上 → 差异就是口径争议的证据，拿到口径确认会上比争论有效。

    ⚠️ 需先由信息科确认 ITEM_CODE 与本产品指标码的映射关系（尚未配置）。
    """
    authz.require_admin(request)
    from app.core import meta_db as db
    st = db.get_source_state("报表层")
    return {
        "domain": domain,
        "status": "pending_config",
        "how": "配置 ITEM_CODE ↔ 指标码 映射后即可自动比对",
        "benchmark_source": "REPORT.RPT_DEPT_DAY（科室 × 日 × 指标项）",
        "benchmark_state": st,
        "note": "该报表层实测止于 2024-10，仅能用于历史区间对账，不能作为现状基准",
    }
