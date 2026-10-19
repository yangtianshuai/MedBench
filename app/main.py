# -*- coding: utf-8 -*-
"""医院智能问数 · 服务端入口。

两个前端入口，职责分开：
    /        问数客户端（只给人问问题、看数）
    /admin   管理端（配模型、配数据源、管语义层与口径）
    /docs    接口文档

启动：
    $env:ASK_HIS_PASSWORD="***"        # 只读账号，禁用应用账号
    uvicorn app.main:app --host 127.0.0.1 --port 8000

模型 / 数据源等可在管理端配置并落盘（var/runtime_config.yaml）；
环境变量优先级更高，用于生产注入密钥。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.core import identity, meta_db, runtime_config, security_gate, session
from app.routers import (admin, analysis, ask, chat, ops, reports, security,
                         semantic, trust)

# ── 控制台编码加固（Windows 中文环境）────────────────────────────
# 启动日志里带 ⚠ 等符号，而 Windows 控制台默认 GBK 编码时 print 会抛
# UnicodeEncodeError —— 一行日志直接把应用启动搞崩（容器里是 UTF-8，所以没暴露）。
# 只放宽错误处理：不可编码字符降级为 '?'，绝不因为日志起不来。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

WEB_DIR = Path(__file__).resolve().parent / "web"

# 本地账号「首登必须改密」期间仍可访问的接口：只放"改密 / 看自己是谁 / 登出"。
# 少放一个会把人锁在门外（连改密页都调不通），多放一个就等于留了绕过口子。
_MUST_CHANGE_ALLOW = {
    "/api/security/auth/local/password/change",
    "/api/security/auth/local/status",
    "/api/security/auth/local/login",
    "/api/security/session",
    "/api/security/logout",
    "/api/security/me",
}

app = FastAPI(
    title="医院智能问数",
    version="0.1.0",
    description="自然语言问数 · 口径优先 · 只读取数",
)

for r in (ask.router, chat.router, trust.router, analysis.router, reports.router,
          semantic.router, security.router, ops.router, admin.router):
    app.include_router(r)


@app.middleware("http")
async def _identity_guard(request, call_next):
    """身份入口守卫（docs/12 §3.4 / §7 A3/A4，见 app/core/security_gate.py）。

    两道：
      1) demo 模式的远端兜底 —— 启动闸门读的是命令行 `--host`，万一是以编程方式
         启动（argv 里没有它），第一道门就失了效。这里按**真实对端地址**再判一次。
      2) **剥离不可信来源的身份头**（A3）—— 应用里读头的代码不止一处，
         任何一处直接读 `x-auth-user` 都会变成伪造入口。在入口删掉它，
         是唯一不依赖"每处都记得校验"的做法。
    """
    host = request.client.host if request.client else None
    reason = security_gate.demo_remote_reject(host)
    if reason:
        return JSONResponse({"detail": reason}, status_code=403)
    if security_gate.should_strip_identity_headers(host):
        drop = {h.lower() for h in identity.TRUSTED_HEADERS} | {identity.USER_FIELD.lower()}
        request.scope["headers"] = [
            (k, v) for (k, v) in request.scope.get("headers", [])
            if k.decode("latin-1").lower() not in drop]
        # Starlette 会把首次访问的 headers 缓存在 _headers 上；不重置的话
        # 剥离只改了 scope，读到的还是旧头（自测里能跑出来，运行期很难发现）。
        request.__dict__.pop("_headers", None)

    # ③ 本地账号首登必须改密：在**入口**拦一次，而不是给每个路由各写一遍 ——
    #    漏一个路由就等于允许"拿着初始口令的人"继续出数。
    #    ★ 只拦 /api/ 前缀：静态页面与改密弹窗本身要能打开。
    if identity.local_login_enabled():
        path = request.url.path
        if path.startswith("/api/") and path not in _MUST_CHANGE_ALLOW:
            tok = session.read_token(request)
            s = session.resolve(tok, touch=False) if tok else {}
            if s.get("ok") and (s.get("auth_source") or "") == "local":
                row = meta_db.get_local_user(s["user_id"])
                if row and row.get("must_change"):
                    return JSONResponse(
                        {"detail": "首次登录必须修改口令后才能使用系统（初始口令只用于换自己的口令）",
                         "must_change": True,
                         "change_url": "/api/security/auth/local/password/change"},
                        status_code=409)
    return await call_next(request)


@app.on_event("startup")
def _startup() -> None:
    # ★ 安全闸门放在最前：demo 模式被绑到非环回地址时**直接起不来**，
    #   而不是"起得来、跑得欢、界面上一切正常"（那正是最危险的形态）。
    gate = security_gate.preflight()
    print(f"[security] auth_mode={gate['auth_mode']} bind_host={gate['bind_host']}")
    # 身份配置自检：三类"系统照常跑、就是没人能登录"的配错在这里当场说清
    for w in security_gate.config_warnings(gate["auth_mode"]):
        print("[security] " + w)
    settings.ensure_dirs()
    meta_db.init_db()
    # RLS / 授权台账自检（要在 meta_db 初始化之后：它要读台账）
    for w in security_gate.rls_warnings():
        print("[security] " + w)
    # 本地账号兜底（docs/12 §3.6）：要在 init_db 之后，它读表
    for w in security_gate.local_warnings():
        print("[security] " + w)
    # 审计独立存储（docs/12 §6.2）：老库的历史审计行一次性搬走（幂等），并自检密钥与链锚
    from app.core import audit as audit_store
    print(f"[audit] {audit_store.migrate_from_meta().get('note')}")
    for w in security_gate.audit_warnings():
        print("[audit] " + w)
    # 清理早已无用的会话行（撤销记录保留 7 天宽限期，见 session.GRACE_SECONDS）
    print(f"[security] 已清理过期会话 {session.purge_expired()} 条")
    # 管理端保存的运行时配置在启动时生效。
    # 此时管线尚未构建，所以不触发 reload；env 已接管的项不会被覆盖。
    runtime_config.apply_to_settings(reload_pipeline=False, rebuild_conn=False)
    # 密钥不留盘（F6 · docs/12 §1.2 S6）：把盘上的明文密钥搬进进程内存并净化文件。
    moved = runtime_config.migrate_secrets_to_memory()
    if moved:
        print("[security] 已把以下明文密钥从 var/runtime_config.yaml 移入进程内存："
              + ", ".join(moved)
              + " —— 重启后需用环境变量注入（ASK_LLM_API_KEY / ASK_HIS_PASSWORD）")
    # ★ 让"当前到底用的哪个模型"在启动日志里可见。
    #   降级链是「真模型 > 规则 NLU > 无」，三种模式的行为差别很大
    #   （实测：真模型把「2023年」译成 end=2023-12-31 漏一天，规则 NLU 译成 2024-01-01），
    #   但都不报错。不打印出来，运维会把降级结果当成模型结果 —— 排查会走很远。
    print("[llm] " + runtime_config.llm_mode_desc())


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "医院智能问数", "version": "0.1.0",
            "data_source": settings.data_source}


@app.get("/api/modules")
def modules() -> dict:
    """八个功能模块的自我声明 —— 前端据此渲染导航。"""
    return {"modules": [
        {"code": "F1", "name": "对话问数", "endpoint": "/api/ask"},
        {"code": "F1.9", "name": "会话与历史", "endpoint": "/api/chat"},
        {"code": "F2", "name": "可信与口径透明", "endpoint": "/api/trust"},
        {"code": "F3", "name": "分析能力", "endpoint": "/api/analysis"},
        {"code": "F4", "name": "报表与驾驶舱", "endpoint": "/api/reports"},
        {"code": "F5", "name": "语义层后台", "endpoint": "/api/semantic"},
        {"code": "F6", "name": "权限与安全", "endpoint": "/api/security"},
        {"code": "F7", "name": "运营与闭环", "endpoint": "/api/ops"},
        {"code": "F8", "name": "平台管理", "endpoint": "/api/admin"},
    ]}


if WEB_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/")
    def index():
        """问数客户端。"""
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/admin")
    def admin_console():
        """管理端（与客户端分开：这里是配置与治理，不是问数）。"""
        f = WEB_DIR / "admin.html"
        if not f.exists():
            return JSONResponse({"message": "管理端未部署"}, status_code=404)
        return FileResponse(f)
else:
    @app.get("/")
    def index():
        return JSONResponse({"message": "前端未部署", "docs": "/docs"})
