# -*- coding: utf-8 -*-
"""F8 平台管理 —— 数据源、时效、模型、语义层版本。"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.config import settings
from app.core import authz, engine_bridge, freshness, identity, meta_db
from app.core import registry as reg
from app.core.his import his

router = APIRouter(prefix="/api/admin", tags=["F8 平台管理"])

# 数据源切换的二次确认口令（docs/12 §8「管理端鉴权 + 数据源改动二次确认」）：
# 改数据源会改变**所有**问数的结果口径（his 与 ads 不是同一份数据），
# 因此不接受"顺手点一下"。要改必须显式带上这句确认。
DATASOURCE_CONFIRM = "我确认切换数据源"


@router.get("/health")
def health(request: Request) -> Dict[str, Any]:
    authz.require_admin(request)
    out: Dict[str, Any] = {"service": "ok"}

    # 语义层
    try:
        r = reg.load_registry()
        out["semantic"] = {"ok": True, "version": r.get("version"),
                           "metrics": len(r.get("metrics") or [])}
    except Exception as e:
        out["semantic"] = {"ok": False, "error": str(e)}

    # 源库（懒连接，失败不影响服务启动）
    try:
        out["source"] = his.ping()
    except Exception as e:
        out["source"] = {"ok": False, "error": str(e)}

    # 模型
    out["model"] = {"configured": bool(settings.llm_base_url and settings.llm_api_key),
                    "base_url": settings.llm_base_url or None,
                    "model": settings.llm_model or None,
                    "mock": os.getenv("ASK_LLM_MOCK") == "1",
                    "note": "医院场景必须指向院内网私有化部署的模型服务"}

    # 元数据库
    try:
        meta_db.init_db()
        out["meta_db"] = {"ok": True, "path": settings.meta_db_path}
    except Exception as e:
        out["meta_db"] = {"ok": False, "error": str(e)}

    out["ok"] = all(v.get("ok", True) for k, v in out.items()
                    if isinstance(v, dict) and "ok" in v)
    return out


@router.get("/sources")
def sources(request: Request) -> List[Dict[str, Any]]:
    """源域时效（每行带 `source` 字段）。数据源注册现状见 /sources/registry。"""
    authz.require_admin(request)
    return freshness.state()


@router.get("/sources/registry")
def sources_registry(request: Request) -> Dict[str, Any]:
    """数据源注册表（F8.1）—— 多源可配的入口。

    ★ 本端点回答「有哪些源、配好了没」；/sources 回答「数据到几号了」。
    ★ 只回显字段名，**绝不回显连接值**（尤其密码）。
    """
    authz.require_admin(request)
    from app.core import sources as src_reg
    return {
        "sources": src_reg.status(),
        "extract_sources": src_reg.by_layer("extract"),
        "serve_sources": src_reg.by_layer("serve"),
        "serve_source_active": settings.data_source,
        "registry_path": settings.sources_path,
        "note": "多源接入在抽取层：此处登记源、做只读盘点和时效探测；"
                "抽取汇聚到 ADS 后由问数引擎单源查询（问数不做跨源查询）",
    }


@router.post("/sources/probe")
def probe(request: Request) -> Dict[str, Any]:
    """探测全部源域时效（只读，只跑 SELECT MAX(日期列)）。

    ★ 按「域所属的源」路由；未接入的源记为 `unconfigured`，不静默跳过。
    """
    authz.require_admin(request)
    try:
        res = freshness.probe_all()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "results": res,
            "stale": [r["domain"] for r in res if r.get("freshness") == "stale"],
            "unconfigured": [r["domain"] for r in res
                             if r.get("freshness") == "unconfigured"],
            "source_of": {r["domain"]: r.get("source") for r in res}}


@router.get("/models")
def models(request: Request) -> Dict[str, Any]:
    """模型配置状态。绝不返回 API Key。"""
    authz.require_admin(request)
    return {"configured": bool(settings.llm_base_url and settings.llm_api_key),
            "base_url": settings.llm_base_url or None,
            "model": settings.llm_model or None,
            "timeout": settings.llm_timeout,
            "mock_mode": os.getenv("ASK_LLM_MOCK") == "1"}


@router.get("/registry")
def registry_info(request: Request) -> Dict[str, Any]:
    authz.require_admin(request)
    r = reg.load_registry()
    return {"version": r.get("version"),
            "metrics": len(r.get("metrics") or []),
            "measures": len(r.get("measures") or []),
            "dimensions": len(r.get("dimensions") or []),
            "domains": reg.metric_domains(),
            "path": settings.registry_path}


@router.post("/pipeline/reload")
def reload_pipeline(request: Request) -> Dict[str, Any]:
    """语义层热更新后重建管线（同时清 registry 缓存并重建 pipeline）。"""
    authz.require_admin(request)
    try:
        from app.core import registry as _reg
        from app.core import physical_map as _pm
        # 顺序要紧：先强制重读物理映射，再重建 registry，最后重建管线。
        # 只刷 registry 不刷 physical_map，热更新会静默失效（改了配置却没生效）。
        _pm.load(force=True)
        _pm.load_profile(force=True)
        _reg.load_registry(force=True)
        engine_bridge.get_pipeline(force=True)
        return {"ok": True, "version": reg.registry_version(),
                "metrics": len(_reg.load_registry().get("metrics") or []),
                "physical_map": list((_pm.load().get("measures") or {}).keys())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── 口径配置（院方业务配置的可视化管理）──────────────────
# 这一层是「院方口径」，与指标定义分开：指标定义全院通用，
# 口径规则每家医院不同，所以放在 hospital_profile.yaml 里让管理员自己改。

@router.get("/caliber")
def caliber(request: Request) -> Dict[str, Any]:
    """口径规则与科室分组现状。"""
    authz.require_admin(request)
    from app.core import physical_map as pm
    prof = pm.load_profile()
    rules = [{
        "code": r.get("code"),
        "name": r.get("name"),
        "enabled": bool(r.get("enabled")),
        "applies_to": r.get("applies_to") or [],
        "where": r.get("where"),
        "note": (r.get("note") or "").strip(),
        "his_only": bool(r.get("his_only")),
    } for r in (prof.get("caliber_rules") or [])]
    groups = [{
        "code": g.get("code"),
        "name": g.get("name"),
        "source": g.get("source"),
        "group_count": len(g.get("groups") or {}),
        "dept_count": sum(len(v) for v in (g.get("groups") or {}).values()),
    } for g in (prof.get("dept_groups") or [])]
    return {"hospital": prof.get("hospital"),
            "profile_path": settings.hospital_profile_path,
            "data_source": settings.data_source,
            "rules": rules, "dept_groups": groups}


class RuleToggle(BaseModel):
    code: str
    enabled: bool


@router.post("/caliber/rule")
def toggle_rule(req: RuleToggle, request: Request) -> Dict[str, Any]:
    """开关一条口径规则：改配置 → 热重载 → 立即生效。

    ★ 这是**改口径**：会影响所有问数的数值。必须管理员，且（v0.18 起）
      由 `authz` 统一判定而不是靠前端藏按钮。

    为什么改文本而不是 yaml.dump：配置文件里带着「这条规则为什么
    默认关闭」的注释，dump 一遍注释全丢，院方就看不懂了。
    """
    authz.require_admin(request)
    import re as _re
    from app.core import physical_map as pm
    path = settings.hospital_profile_path
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    pat = _re.compile(
        rf"(  - code: {_re.escape(req.code)}\b.*?enabled: )(true|false)", _re.S)
    new_text, n = pat.subn(
        lambda m: m.group(1) + ("true" if req.enabled else "false"), text, count=1)
    if not n:
        raise HTTPException(status_code=404, detail=f"未找到口径规则 {req.code}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(new_text)
    pm.load_profile(force=True)
    reg.load_registry(force=True)
    engine_bridge.get_pipeline(force=True)
    return {"ok": True, "code": req.code, "enabled": req.enabled}


# ── 运行时配置（模型 / 数据源）────────────────────────────
# 管理端可改、可落盘、可热生效。密钥只写不读（GET 一律脱敏）。

@router.get("/config")
def get_config(request: Request) -> Dict[str, Any]:
    """当前配置（密钥脱敏）+ 哪些项已被环境变量接管。"""
    authz.require_admin(request)
    from app.core import runtime_config as rc
    return rc.masked()


@router.put("/config/llm")
def put_llm_config(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """保存模型配置并重建管线，立即生效。"""
    authz.require_admin(request)
    from app.core import runtime_config as rc
    rc.update("llm", payload or {})
    out = rc.apply_to_settings()
    return {"ok": True, **out}


@router.put("/config/datasource")
def put_datasource_config(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """保存数据源配置并重建连接，立即生效。

    ★ **二次确认**（docs/12 §8）：切换数据源会改变所有问数的结果口径
      （his = 直连镜像，ads = 本地宽表，两者不是同一份数据）。
      必须显式带上 `confirm: "我确认切换数据源"`，否则 400。
      这一条防的不是攻击者（他可以直接改配置），而是防"手滑"：
      口径悄悄换了、界面看着一切正常，是最难查的一类事故。
    """
    authz.require_admin(request)
    if (payload or {}).get("confirm") != DATASOURCE_CONFIRM:
        raise HTTPException(
            status_code=400,
            detail=f"切换数据源需要二次确认：请在请求体里带上 confirm=\"{DATASOURCE_CONFIRM}\"")
    from app.core import runtime_config as rc
    # confirm 只是"过闸口令"，不能落盘到配置里（否则配置文件里多一个没用的键）
    data = {k: v for k, v in (payload or {}).items() if k != "confirm"}
    rc.update("datasource", data)
    out = rc.apply_to_settings()
    return {"ok": True, **out}


@router.post("/config/llm/test")
def test_llm(request: Request, payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """测模型连通性。

    用界面上刚填的值（未保存也能测）发一个最小请求，
    避免「保存了才发现地址写错」这种来回折腾。
    """
    authz.require_admin(request)
    from app.core import runtime_config as rc
    cfg = rc.load().get("llm") or {}
    payload = payload or {}
    base_url = (payload.get("base_url") or cfg.get("base_url") or "").rstrip("/")
    api_key = payload.get("api_key") or cfg.get("api_key") or ""
    model = payload.get("model") or cfg.get("model") or ""
    if not base_url or not api_key:
        return {"ok": False, "error": "base_url 与 api_key 必填"}
    import json as _json
    import urllib.error
    import urllib.request
    url = f"{base_url}/chat/completions"
    body = {"model": model or "default",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8}
    req = urllib.request.Request(
        url, data=_json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        got = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return {"ok": True, "url": url, "reply": (got or "")[:80],
                "model": data.get("model") or model}
    except urllib.error.HTTPError as e:
        return {"ok": False, "url": url, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"ok": False, "url": url, "error": f"{type(e).__name__}: {e}"}


@router.post("/config/datasource/test")
def test_datasource(request: Request, payload: Dict[str, Any] = None) -> Dict[str, Any]:
    """测数据源连通性：HIS 走只读 ping，ADS 走本地文件可读性。"""
    authz.require_admin(request)
    from app.core import runtime_config as rc
    cfg = rc.load().get("datasource") or {}
    payload = payload or {}
    active = payload.get("active") or cfg.get("active") or "his"
    if active == "ads":
        import os
        path = (payload.get("ads") or {}).get("path") or \
               (cfg.get("ads") or {}).get("path") or settings.ads_path
        if not os.path.exists(path):
            return {"ok": False, "active": "ads", "error": f"文件不存在：{path}"}
        import sqlite3
        try:
            con = sqlite3.connect(path)
            n = con.execute(
                "SELECT COUNT(*) FROM dwd_fact_inpatient_discharge").fetchone()[0]
            con.close()
            return {"ok": True, "active": "ads", "path": path,
                    "rows": n, "dialect": "sqlite"}
        except Exception as e:
            return {"ok": False, "active": "ads", "error": str(e)}
    try:
        return {"ok": True, "active": "his", **his.ping()}
    except Exception as e:
        return {"ok": False, "active": "his", "error": str(e)}


@router.post("/config/reset")
def reset_config(request: Request) -> Dict[str, Any]:
    """清空运行时配置（回落到环境变量/默认值）。"""
    authz.require_admin(request)
    from app.core import runtime_config as rc
    rc.save({})
    out = rc.apply_to_settings()
    return {"ok": True, **out}


# ────────────────────────────────────────────────────────────
# 物理映射可视化编辑（F5.3）
#
# 为什么改映射需要二次确认：它和"切换数据源"是同一类变更 —— 改完所有指标的
# 数字都可能变，而界面依旧一切正常。手滑的代价是全院看到错数。
# ────────────────────────────────────────────────────────────
MAP_CONFIRM = "我确认更新物理映射"


@router.get("/physical-map")
def get_physical_map(request: Request) -> Dict[str, Any]:
    """当前物理映射 + 覆盖度诊断 + 可回滚的备份列表。"""
    authz.require_admin(request)
    from app.core import physical_map_store as store
    info = store.read()
    try:
        diag = store.diagnose(store.parse(info["text"] or "{}"))
    except store.MapValidationError as e:
        diag = {"ok": False, "errors": e.errors}
    return {**info, "diagnose": diag, "backups": store.backups(),
            "confirm_phrase": MAP_CONFIRM,
            "note": "诊断只做配置层判断：『已映射』不等于库里确有该表/该列"}


@router.post("/physical-map/validate")
def validate_physical_map(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """保存前预检：结构错误 + 覆盖度影响。不落盘。"""
    authz.require_admin(request)
    from app.core import physical_map_store as store
    text = str((payload or {}).get("text") or "")
    try:
        data = store.parse(text)
    except store.MapValidationError as e:
        return {"ok": False, "errors": e.errors}
    errs = store.validate(data)
    out: Dict[str, Any] = {"ok": not errs, "errors": errs}
    if not errs:
        out["diagnose"] = store.diagnose(data)
    return out


@router.put("/physical-map")
def put_physical_map(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """保存映射：校验 → 备份 → 原子写入 → 热加载重建管线。"""
    p = authz.require_admin(request)
    from app.core import physical_map_store as store
    payload = payload or {}
    if payload.get("confirm") != MAP_CONFIRM:
        raise HTTPException(
            status_code=400,
            detail=f"更新物理映射需要二次确认：请在请求体里带上 confirm=\"{MAP_CONFIRM}\"")
    actor = str(p.get("user_id") or "")
    try:
        return store.save(str(payload.get("text") or ""), actor=actor,
                          note=str(payload.get("note") or ""))
    except store.MapValidationError as e:
        raise HTTPException(status_code=400, detail={"errors": e.errors}) from e


@router.get("/physical-map/backups")
def list_map_backups(request: Request) -> List[Dict[str, Any]]:
    authz.require_admin(request)
    from app.core import physical_map_store as store
    return store.backups()


@router.post("/physical-map/restore")
def restore_map(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """回滚到指定备份（备份本身也要过校验，坏档不会被写回去）。"""
    p = authz.require_admin(request)
    from app.core import physical_map_store as store
    name = str((payload or {}).get("name") or "")
    actor = str(p.get("user_id") or "")
    try:
        return store.restore(name, actor=actor)
    except store.MapValidationError as e:
        raise HTTPException(status_code=400, detail={"errors": e.errors}) from e


# ────────────────────────────────────────────────────────────
# 测试数据一键清理（维护 / 交付前）
#
# 为什么需要二次确认（并且是**手打短语**而不是勾选框）：这一步不可撤销 ——
# 清掉的是审计、会话与反馈，而它们的价值恰恰在"事后"。勾选框会被顺手点上，
# 手打短语不会。
# ────────────────────────────────────────────────────────────
@router.get("/maintenance/inventory")
def maintenance_inventory(request: Request) -> Dict[str, Any]:
    """清理前现状：每组各有多少条（界面据此展示"将清掉 N 条"）。"""
    authz.require_admin(request)
    from app.core import maintenance
    return maintenance.inventory()


@router.post("/maintenance/test-data/clear")
def maintenance_clear(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """按分组清理测试数据（账号组会保留调用者自己的账号）。"""
    p = authz.require_admin(request)
    from app.core import maintenance
    payload = payload or {}
    if (payload.get("confirm") or "") != maintenance.CLEAR_CONFIRM:
        raise HTTPException(
            status_code=400,
            detail=(f"清理测试数据需要二次确认：请在请求体里带上 "
                    f"confirm=\"{maintenance.CLEAR_CONFIRM}\""))
    scopes = payload.get("scopes")
    if isinstance(scopes, str):
        scopes = [scopes]
    res = maintenance.clear(list(scopes or []), actor=str(p.get("user_id") or "admin"),
                            keep_user=str(p.get("user_id") or ""),
                            ip=identity.client_ip(request))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    return res
