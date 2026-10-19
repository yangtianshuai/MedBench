# -*- coding: utf-8 -*-
"""F6 权限与安全 —— 安全无"简版"。"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings
from app.core import (authz, authz_store, authz_sync, identity, local_accounts,
                      meta_db, permission)
from app.core import session as session_store
from app.core.his import his

router = APIRouter(prefix="/api/security", tags=["F6 权限与安全"])

# 登录限速（进程内滑动窗口）：账号锁定防"同一账号爆破"，这层防"换账号撞库"。
# ★ 只统计**失败**：医院里全院常常共用一个出口 IP，把成功登录也计数会让
#   早高峰的正常登录被 429 挡住（验收里就是这么被抓到的）。
# ★ 单实例部署够用；将来上多副本必须换成 Redis —— 写在这里，避免以后误以为它是全局的。
_LOGIN_HITS: Dict[str, List[float]] = {}
LOGIN_WINDOW = 60.0
LOGIN_MAX_PER_WINDOW = 30


def _login_throttled(ip: Optional[str]) -> bool:
    key = ip or "-"
    now = time.time()
    hits = [t for t in _LOGIN_HITS.get(key, []) if now - t < LOGIN_WINDOW]
    _LOGIN_HITS[key] = hits
    return len(hits) >= LOGIN_MAX_PER_WINDOW


def _note_login_failure(ip: Optional[str]) -> None:
    key = ip or "-"
    hits = _LOGIN_HITS.setdefault(key, [])
    hits.append(time.time())
    _LOGIN_HITS[key] = hits[-50:]


def _local_login_info() -> Dict[str, Any]:
    """本地登录入口状态：前端据此决定是否渲染表单（**不靠猜**）。"""
    mode = (settings.auth_local or "off").strip().lower()
    return {"enabled": identity.local_login_enabled(), "mode": mode,
            # 走 `pwd_min_len()` 而不是直接读配置：8 位是代码里的硬下限，
            # 前端提示的长度必须与后端校验**同源**，否则界面说"至少 4 位"、后端拒 8 位以下。
            "pwd_min_len": local_accounts.pwd_min_len(),
            "note": ("本地账号口令登录（兜底通道）：登录只证明工号，"
                     "权限仍按台账解析（docs/12 §3.6）"
                     if identity.local_login_enabled()
                     else "未开启本地登录（ASK_AUTH_LOCAL=off）")}


@router.get("/session")
def session(request: Request, user_id: str = "guest") -> Dict[str, Any]:
    """会话与**身份来源**（F6.1 / docs/12 §3）。

    存在的意义：客户端的身份条不再由前端自选决定授权。
    - demo：服务端显式声明「这是演示身份」并给出账号清单，前端才显示切换器；
    - gateway / sso：不返回任何演示账号；未登录时返回 `need_login` 与登录地址，
      前端据此显示登录入口（而不是猜一个身份出来）。
    """
    mode = settings.auth_mode
    base: Dict[str, Any] = {"auth_mode": mode, "demo_accounts": [],
                            "local_login": _local_login_info(),
                            # 没有任何账号时，客户端据此渲染「创建管理员账号」而不是登录框：
                            # 否则交付现场会卡在"账号表是空的、登录页也登不进去"。
                            "bootstrap": local_accounts.bootstrap_state()}
    if mode == "demo":
        p = authz.principal(request, user_id)
        return {**base, "identity_from": "demo", "user": p,
                "demo_accounts": permission.list_demo_accounts(),
                "need_login": False,
                "note": identity.identity_note(p)}
    r = identity.resolve(request, None)
    login = identity.login_url(request)
    if not r.get("ok"):
        # ⚠️ 这里**不返回 401**：客户端首屏就要渲染"去登录"，返回 401 会让
        #    前端先报错再想办法 —— 状态码是给机器看的，这个接口是给人看的。
        return {**base, "identity_from": "server", "user": None,
                "need_login": r.get("reason") == "login_required",
                "login_url": login.get("url"), "login_config": login,
                "note": r.get("note", "")}
    out = {**base, "identity_from": "server", "user": r["user"], "need_login": False,
           "login_url": None, "note": identity.identity_note(r["user"])}
    # 本地账号首登必须改密：客户端据此直接弹改密框（服务端中间件也会把其它接口拦成 409）。
    # 两处都做不是重复 —— 界面负责「别让人打着字突然吃 409」，中间件负责「绕不过去」。
    if (r["user"].get("identity_source") or "") == "local":
        row = meta_db.get_local_user(r["user"]["user_id"]) or {}
        out["must_change"] = bool(row.get("must_change"))
    return out


# ── 本地账号兜底（F6.1 · docs/12 §3.6）──
#   ★ 它只解决"你是谁"；能看哪些科室仍由 auth_grant 台账决定（两者独立维护）。
@router.get("/auth/local/status")
def local_status() -> Dict[str, Any]:
    """本地登录入口状态（**公开**：登录页要据此决定是否渲染表单）。"""
    return _local_login_info()


@router.get("/bootstrap/status")
def bootstrap_status() -> Dict[str, Any]:
    """首次初始化入口状态（**公开**：没有账号时登录页要据此渲染建号表单）。"""
    return local_accounts.bootstrap_state()


@router.post("/bootstrap/admin")
def bootstrap_admin(request: Request, payload: Dict[str, Any]) -> JSONResponse:
    """创建**第一个**管理员账号（只在账号表为空时可用），并直接登录。

    ★ 为什么需要一个公开的写接口：交付现场常常没有命令行（exe / 容器），
      账号表为空时 `--create-admin` 走不通，系统就成了"谁都进不去、也建不了号"。
    ★ 为什么它不构成"任何人可创建管理员"的洞：账号表非空即返回 400，
      建成第一个后入口永久关闭；口令策略、失败限速、身份事件留痕与登录同样对待。
    """
    ip = identity.client_ip(request)
    if _login_throttled(ip):
        raise HTTPException(status_code=429, detail="操作过于频繁，请稍后再试")
    payload = payload or {}
    res = local_accounts.create_first_admin(
        payload.get("user_id") or "", payload.get("name") or "",
        payload.get("password") or "", payload.get("password2"),
        ip=ip)
    if not res.get("ok"):
        _note_login_failure(ip)
        raise HTTPException(status_code=400, detail=res.get("error") or "初始化失败")

    uid = res["user_id"]
    p = permission.resolve_user(uid)
    s = session_store.create(uid, p.get("role"), auth_mode=settings.auth_mode,
                             auth_source="local", client_ip=ip,
                             user_agent=request.headers.get("user-agent"),
                             is_admin=permission.is_admin(p))
    resp = JSONResponse({"ok": True, "user_id": uid, "role": res.get("role"),
                         "is_admin": permission.is_admin(p),
                         "expires_at": s["expires_at"],
                         "note": res.get("note")})
    session_store.set_cookie(resp, s["token"], s["absolute_seconds"])
    return resp


@router.post("/auth/local/login")
def local_login(request: Request, payload: Dict[str, Any]) -> JSONResponse:
    """本地账号登录：校验口令 → 签发与 SSO **同一种**服务端会话 Cookie。

    失败原因照实返回，但**不区分"账号不存在"与"口令错"** ——
    区分开就等于把登录页变成工号枚举器（内部人也能借它枚举工号）。
    """
    info = _local_login_info()
    if not info["enabled"]:
        raise HTTPException(status_code=404,
                            detail="本地登录未开启（ASK_AUTH_LOCAL=off）")
    ip = identity.client_ip(request)
    if _login_throttled(ip):
        meta_db.log_auth_event("local_login_throttled", None, "local", "local",
                               f"同一来源 60 秒内失败超过 {LOGIN_MAX_PER_WINDOW} 次", ip)
        raise HTTPException(status_code=429, detail="登录失败次数过多，请稍后再试")

    uid = (payload.get("user_id") or "").strip()
    r = local_accounts.login(uid, payload.get("password") or "")
    if not r.get("ok"):
        _note_login_failure(ip)
        meta_db.log_auth_event("local_login_failed", uid or None, "local", "local",
                               f"{r.get('reason')}: {r.get('note')}", ip)
        raise HTTPException(status_code=401, detail=r.get("note") or "登录失败")

    p = permission.resolve_user(uid)
    s = session_store.create(uid, p.get("role"), auth_mode="local", auth_source="local",
                             client_ip=ip, user_agent=request.headers.get("user-agent"),
                             is_admin=permission.is_admin(p))
    meta_db.log_auth_event("local_login", uid, "local", "local",
                           f"口令校验通过；must_change={bool(r.get('must_change'))}", ip)
    resp = JSONResponse({"ok": True, "user_id": uid, "role": p.get("role"),
                         "must_change": bool(r.get("must_change")),
                         "pwd_expired": bool(r.get("pwd_expired")),
                         "expires_at": s["expires_at"],
                         "note": "登录成功；权限按授权台账解析（登录只证明工号）"})
    session_store.set_cookie(resp, s["token"], s["absolute_seconds"])
    return resp


@router.post("/auth/local/password/change")
def local_change_password(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """本人改密（`must_change=1` 时这是唯一可用的写接口）。"""
    p = authz.principal(request)
    if (p.get("identity_source") or "") != "local":
        raise HTTPException(status_code=403,
                            detail="当前身份不是本地账号；改密请走院方统一身份平台")
    res = local_accounts.change_password(p["user_id"],
                                         payload.get("old_password") or "",
                                         payload.get("new_password") or "")
    if not res.get("ok"):
        meta_db.log_auth_event("local_pwd_change_failed", p["user_id"], "local", "local",
                               str(res.get("error")), identity.client_ip(request))
        raise HTTPException(status_code=400, detail=res.get("error"))
    meta_db.log_auth_event("local_pwd_changed", p["user_id"], "local", "local",
                           "用户本人改密", identity.client_ip(request))
    return res


@router.get("/local/users")
def local_users(request: Request, include_disabled: bool = True) -> Dict[str, Any]:
    """本地账号清单（管理员；**不含口令哈希**）。"""
    authz.require_admin(request)
    return {"users": local_accounts.list_users(include_disabled=include_disabled),
            "stats": local_accounts.stats(),
            "note": "本地账号只证明身份；能看哪些科室由授权台账决定（两者独立维护）"}


@router.post("/local/users")
def local_user_create(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """建号（管理员）。默认**停用**：建好就能用，等于没人复核过。"""
    p = authz.require_admin(request)
    res = local_accounts.create_user(
        payload.get("user_id") or "", payload.get("name") or "",
        actor=p["user_id"], reason=payload.get("reason") or "",
        password=payload.get("password") or None,
        valid_days=payload.get("valid_days"), enabled=bool(payload.get("enabled")))
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    # ★ 审计只记"建了谁、依据是什么"，**不记初始口令** —— 日志里出现口令等于口令泄露
    meta_db.log_auth_event("local_user_created", res["user_id"], "local", "admin",
                           f"by {p['user_id']}；status={res['status']}；"
                           f"reason={payload.get('reason')}", identity.client_ip(request))
    return res


@router.post("/local/users/password")
def local_user_reset_password(request: Request,
                              payload: Dict[str, Any]) -> Dict[str, Any]:
    """重置口令（管理员）：一次性初始口令 + 强制改密 + 撤销该人全部会话。"""
    p = authz.require_admin(request)
    res = local_accounts.reset_password(payload.get("user_id") or "",
                                        actor=p["user_id"],
                                        reason=payload.get("reason") or "")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    meta_db.log_auth_event("local_pwd_reset", res["user_id"], "local", "admin",
                           f"by {p['user_id']}；revoked={res.get('revoked_sessions')}；"
                           f"reason={payload.get('reason') or '-'}",
                           identity.client_ip(request))
    return res


@router.post("/local/users/status")
def local_user_status(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """启用 / 停用（管理员）。停用会**立刻**踢掉全部会话。"""
    p = authz.require_admin(request)
    res = local_accounts.set_status(payload.get("user_id") or "",
                                    (payload.get("status") or "").strip(),
                                    actor=p["user_id"],
                                    reason=payload.get("reason") or "")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    ip = identity.client_ip(request)
    meta_db.log_auth_event("local_user_status", res["user_id"], "local", "admin",
                           f"by {p['user_id']} → {res['status']}；"
                           f"revoked={res.get('revoked_sessions', 0)}", ip)
    if res.get("self_enabled"):
        meta_db.log_auth_event("local_self_enabled", res["user_id"], "local", "admin",
                               f"创建人与启用人同为 {p['user_id']}（建议双人复核）", ip)
    return res


@router.post("/local/users/unlock")
def local_user_unlock(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """解锁（管理员；不做自动解锁 —— 自动解锁等于没有锁定）。"""
    p = authz.require_admin(request)
    res = local_accounts.unlock(payload.get("user_id") or "", actor=p["user_id"])
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    meta_db.log_auth_event("local_user_unlocked", res["user_id"], "local", "admin",
                           f"by {p['user_id']}", identity.client_ip(request))
    return res


@router.post("/local/users/renew")
def local_user_renew(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """续期（管理员）：兜底账号必须有失效日，续期是一次显式动作。"""
    p = authz.require_admin(request)
    res = local_accounts.renew(payload.get("user_id") or "", actor=p["user_id"],
                               valid_days=float(payload.get("valid_days") or 365),
                               reason=payload.get("reason") or "")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    meta_db.log_auth_event("local_user_renewed", res["user_id"], "local", "admin",
                           f"by {p['user_id']}；valid_days={payload.get('valid_days')}",
                           identity.client_ip(request))
    return res


@router.get("/login")
def login(request: Request):
    """跳转到院方统一身份平台（CAS / OIDC）。

    参数未配置时**明确说缺哪一项**（docs/12 §3.5 先要东西再动手），
    而不是重定向到一个必然失败、报错看不懂的地址。
    """
    lu = identity.login_url(request)
    if not lu.get("ok"):
        raise HTTPException(status_code=503,
                            detail=f"{lu.get('note')}；缺少：{', '.join(lu.get('missing') or [])}")
    meta_db.log_auth_event("login_redirect", None, settings.auth_mode, lu["protocol"],
                           "跳转统一身份平台", identity.client_ip(request))
    return RedirectResponse(url=lu["url"], status_code=302)


@router.get("/callback")
def callback(request: Request, ticket: Optional[str] = None, code: Optional[str] = None,
             state: Optional[str] = None):
    """SSO 回调：校验票据 → 建**服务端会话** → 写 Cookie → 回到首页。

    失败一律跳回首页并带上原因（浏览器流程里，一个 JSON 401 页面等于"白屏"）。
    """
    r = identity.handle_callback(request)
    if not r.get("ok"):
        meta_db.log_auth_event("login_failed", None, settings.auth_mode,
                               settings.sso_protocol, r.get("error"), identity.client_ip(request))
        return RedirectResponse(url="/?auth_error=" + quote(str(r.get("error"))[:200]),
                                status_code=302)
    # 授权**此刻**重新解析，用于决定会话的空闲阈值（管理端更短）
    p = permission.resolve_user(r["user_id"])
    s = session_store.create(
        r["user_id"], p.get("role"), auth_mode=settings.auth_mode,
        auth_source=settings.sso_protocol, client_ip=identity.client_ip(request),
        user_agent=request.headers.get("user-agent"), is_admin=permission.is_admin(p))
    meta_db.log_auth_event("login", r["user_id"], settings.auth_mode,
                           settings.sso_protocol,
                           f"票据校验通过；身份平台属性：{r.get('attrs')}",
                           identity.client_ip(request))
    resp = RedirectResponse(url="/", status_code=302)
    session_store.set_cookie(resp, s["token"], s["absolute_seconds"])
    return resp


@router.post("/logout")
def logout(request: Request) -> JSONResponse:
    """登出：**服务端立即失效** + 清 Cookie。

    只清 Cookie 不算登出 —— 客户端随时能把旧值写回来。
    ★ 必须返回真正的 Response 对象：写进一个临时对象上的 Cookie 头不会到达浏览器，
      那会变成一个"看起来登出了、实际没登出"的假实现。
    """
    token = session_store.read_token(request)
    done = session_store.revoke(token, "logout") if token else False
    if token:
        meta_db.log_auth_event("logout", None, settings.auth_mode, "session",
                               "用户主动登出" if done else "登出时令牌已失效",
                               identity.client_ip(request))
    resp = JSONResponse({"ok": True, "revoked": done,
                         "note": "服务端会话已失效；仅清 Cookie 不算登出"})
    session_store.clear_cookie(resp)
    return resp


@router.get("/sessions")
def sessions(request: Request, user_id: Optional[str] = None,
             include_revoked: bool = False) -> List[Dict[str, Any]]:
    """在线会话清单（管理端「谁在线」）。

    ★ 返回字段里**没有 token**：会话令牌是凭据，不是可展示的数据。
    """
    authz.require_admin(request)
    return meta_db.list_user_sessions(user_id=user_id, include_revoked=include_revoked)


@router.post("/sessions/revoke")
def revoke_sessions(request: Request, target_user: str) -> Dict[str, Any]:
    """强制下线某账号（转岗 / 离职 / 异常访问，docs/12 §7 B5 的服务端一半）。

    会连同调用者自己的会话一起撤销（除当前会话）—— 管理员踢人时自己不该掉线。
    """
    p = authz.require_admin(request)
    n = session_store.revoke_user(target_user, f"revoked_by:{p.get('user_id')}",
                                  except_token=session_store.read_token(request))
    meta_db.log_auth_event("sessions_revoked", target_user, settings.auth_mode, "admin",
                           f"由 {p.get('user_id')} 强制下线 {n} 个会话",
                           identity.client_ip(request))
    return {"ok": True, "revoked": n, "target_user": target_user}


@router.get("/grants")
def grants(request: Request, user_id: Optional[str] = None,
           include_inactive: bool = False) -> Dict[str, Any]:
    """授权台账（F6.2 · docs/12 §4.2）—— 「谁能看哪些科室、到什么时候、依据是什么」。

    ★ 这是**读**接口，但只有管理员能读：台账就是一张"谁在哪个科室、什么岗位"的名册，
      它本身就是敏感信息（比指标目录更敏感）。
    """
    authz.require_admin(request)
    rows = authz_store.list_grants(include_inactive=include_inactive)
    if user_id:
        rows = [r for r in rows if r["user_id"] == user_id]
    return {"grants": rows, "stats": authz_store.stats(),
            "note": "台账是授权唯一事实来源；roles.yaml 只是角色定义模板（能做什么）"}


@router.post("/grants")
def put_grant(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """授权 / 变更（管理员）。变更 = 撤旧 + 写新 + 留痕，**不原地改**。

    必填 `ref`（依据）：组织架构版本号、工单号或备案号 —— 没有它，
    半年后没人答得出"这个权限是谁给的"（docs/12 §4.4）。
    """
    p = authz.require_admin(request)
    days = payload.get("valid_days")
    valid_to = None
    if days is not None:
        valid_to = time.time() + float(days) * 86400
    elif payload.get("valid_to"):
        valid_to = float(payload["valid_to"])
    res = authz_store.upsert_grant(
        user_id=(payload.get("user_id") or "").strip(),
        role=(payload.get("role") or "").strip(),
        dept_scope=payload.get("dept_scope"),
        campus_scope=payload.get("campus_scope"),
        valid_from=payload.get("valid_from"), valid_to=valid_to,
        source=payload.get("source") or "manual",
        source_ref=(payload.get("ref") or "").strip(),
        actor=p["user_id"], reason=payload.get("reason") or "管理员授权")
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    meta_db.log_auth_event("grant_changed", res["after"]["user_id"], settings.auth_mode,
                           "admin", f"{res['action']} by {p['user_id']}：{res['after']}",
                           identity.client_ip(request))
    return res


@router.post("/grants/revoke")
def revoke_grant(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """撤销授权（离职 / 转岗 / 审计要求）。不删历史行 —— 审计要的是"他曾经有什么"。"""
    p = authz.require_admin(request)
    uid = (payload.get("user_id") or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="user_id 必填")
    res = authz_store.revoke_grant(uid, actor=p["user_id"],
                                   reason=payload.get("reason") or "管理员撤销",
                                   source_ref=(payload.get("ref") or "").strip())
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error"))
    meta_db.log_auth_event("grant_revoked", uid, settings.auth_mode, "admin",
                           f"revoked by {p['user_id']}", identity.client_ip(request))
    return res


@router.get("/grants/log")
def grant_log(request: Request, grantee: Optional[str] = None,
              limit: int = 100) -> Dict[str, Any]:
    """授权变更留痕（只追加 + 哈希链）。与问数审计分开：授权变更 ≠ 数据访问。"""
    authz.require_admin(request)
    return {"log": meta_db.list_authz_log(grantee=grantee, limit=limit),
            "chain": meta_db.verify_authz_chain(),
            "note": "谁改的（actor）与被改的人（grantee）分开记；本地 SQLite 仍可被整链重算"}


@router.post("/grants/sync")
def grant_sync(request: Request, payload: Dict[str, Any]) -> Dict[str, Any]:
    """组织架构同步的**预览/执行**（docs/12 §4.2）。

    `apply=false`（默认）只返回计划：新增/变更/撤销逐条列出。
    ★ 界面默认走预览：授权批量变更不该是"点一下试试"。
    """
    p = authz.require_admin(request)
    source = payload.get("source") or "yaml"
    ref = (payload.get("ref") or "").strip()
    if not ref:
        raise HTTPException(status_code=400, detail="ref 必填（组织架构版本号 / 工单号）")
    # ★ 接口只允许 `yaml` 源：`file` 源要读**请求体给的任意路径**，而解析失败时
    #   还会把文件内容回显到错误里 —— 那等于给管理员开了个任意文件读取口
    #   （`file: var/askdata.db` 就能把库当 CSV 读出来）。
    #   从院方导出文件同步请在服务器上用命令行跑（那里是本机运维操作，不是接口）。
    if source != "yaml":
        raise HTTPException(status_code=400, detail=(
            "界面同步只支持 source=yaml（把 roles.yaml 名单迁进台账）。"
            "从院方组织架构导出文件同步请在服务器上跑 "
            "`python scripts/sync_authz.py --source file --file ... --ref ...`："
            "接口不接受调用方指定的文件路径。"))
    # ★ 批量写授权要二次确认（与数据源切换同一套做法）：防的不是攻击者，是手滑 ——
    #   一次误按可能把一批人的范围改掉，而界面上一切正常。
    if payload.get("apply") and (payload.get("confirm") or "") != "我确认写入授权台账":
        raise HTTPException(status_code=400, detail=(
            "批量写入授权需要二次确认：请传 confirm='我确认写入授权台账'。"
            "先看预览（apply=false），确认每条变更都是预期内的，再执行。"))
    if payload.get("revoke_missing") and (payload.get("confirm") or "") != "我确认写入授权台账":
        raise HTTPException(status_code=400, detail=(
            "撤销名单外人员的授权必须二次确认（不完整的导出会撤掉全院权限）"))
    r = authz_sync.run(source=source, ref=ref, apply=bool(payload.get("apply")),
                       revoke_missing=bool(payload.get("revoke_missing")),
                       valid_days=payload.get("valid_days"))
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail={
            "error": r["error"], "bad": (r.get("bad") or [])[:50],
            "read": r.get("read"), "mapped": r.get("mapped"), "hint": r.get("hint")})
    if r.get("applied"):
        meta_db.log_auth_event("grant_sync", None, settings.auth_mode, source,
                               f"{p['user_id']} applied：{r.get('result')}",
                               identity.client_ip(request))
    return r


@router.get("/authz/mode")
def authz_mode(request: Request) -> Dict[str, Any]:
    """当前授权来源档位与收敛进度（迁移期最该盯的一个数）。"""
    authz.require_admin(request)
    st = authz_store.stats()
    return {"authz_from": st["authz_from"],
            "yaml_only_users": st["yaml_only_users"],
            "hint": "ASK_AUTHZ_FROM=auto 允许回落 roles.yaml；台账覆盖到 100% 后切 table"
                    "（切之前建议先跑一次同步预览）"}


@router.get("/events")
def auth_events(request: Request, limit: int = 100, kind: Optional[str] = None,
                user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """身份事件（登录/登出/票据失败/伪造头被拒）。仅管理员。

    与 `/audit` 分开：那里是"谁问了什么数"，这里是"身份发生过什么"。
    """
    authz.require_admin(request)
    return meta_db.list_auth_events(limit=limit, kind=kind, user_id=user_id)


@router.get("/me")
def me(request: Request, user_id: str = "guest") -> Dict[str, Any]:
    return authz.principal(request, user_id)


@router.get("/roles")
def roles(request: Request) -> List[Dict[str, Any]]:
    """角色定义（含 `visible_metrics` 等授权配置）→ 管理端页面，需管理权限。

    ★ **不返回 `guest`**：它不是"角色"，而是"未知用户的最小权限兜底"。
      列进角色表会诱导管理员去给某个工号授"未授权"（等于批量收权限），
      也会让人以为它是一个可以选择的岗位。兜底逻辑本身不变（12 §4.1）。
    """
    authz.require_admin(request)
    return [r for r in permission.list_roles() if r.get("code") != "guest"]


@router.get("/audit")
def audit(request: Request, limit: int = 100, filter_user: str = None,
          user_id: str = None) -> List[Dict[str, Any]]:
    """审计：谁 / 何时 / 问了什么 / 用了什么口径 / 结果指纹。

    ★ 授权（12 §6.3）：管理员看全量，普通用户**只能看自己的**，
      未授权身份（guest）直接拒绝。此前 `user_id=None` 会返回全院的问答记录 ——
      "谁在什么时候问了什么"本身就是敏感数据。
    """
    p = authz.principal(request, user_id)
    if permission.is_admin(p):
        return meta_db.list_audit(limit=limit, user_id=filter_user)
    if p.get("role") == "guest":
        raise HTTPException(status_code=403,
                            detail="未授权身份不能查询审计；请用院内账号登录（docs/12 §3.4）")
    return meta_db.list_audit(limit=limit, user_id=p["user_id"])


@router.get("/audit/summary")
def audit_summary(request: Request, limit: int = 500) -> Dict[str, Any]:
    """审计汇总属于**全局视图**，仅管理员可见（普通用户看到全院的拦截率等于看到别人的行为）。"""
    authz.require_admin(request)
    rows = meta_db.list_audit(limit=limit)
    by_status: Dict[str, int] = {}
    denied = 0
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        if r["status"] == "denied":
            denied += 1
    return {
        "total": len(rows),
        "by_status": by_status,
        "denied": denied,
        "deny_rate": (denied / len(rows)) if rows else 0.0,
        "note": "越权漏放必须为 0；deny 为正确拦截，不是失败",
    }


@router.get("/audit/verify")
def audit_verify(request: Request, limit: int = 20000) -> Dict[str, Any]:
    """校验审计链 + **链锚**（等保测评必问：你怎么证明日志没被改过）。

    返回里能看出四件事，缺一不可：
      ① `ok/checked` —— 链本身连续且每条都验得过（HMAC，密钥在库外）；
      ② `anchor_ok` —— 与**库外的链锚**对得上（这是**唯一**能发现"删掉尾部记录"
         的东西：只删尾部，剩下的链仍然自洽）；
      ③ `key_id/key_hint` —— 失败时区分「记录被改」与「换了审计密钥」；
      ④ `legacy_rows/legacy_chain` —— 迁移前那段历史的条数与旧算法下的自洽性，
         如实报告，不假装它们也被新链保护过。
    """
    authz.require_admin(request)
    return meta_db.verify_chain(limit=limit)


@router.get("/audit/storage")
def audit_storage(request: Request) -> Dict[str, Any]:
    """审计存储现状（管理员）：库路径 / 纳链条数 / 遗留条数 / 密钥来源 / 链锚数量。

    ★ 为什么把"密钥来源"暴露给管理员看：审计密钥丢了，历史链就**永久无法校验**。
      这不是一个可以埋在配置文件里的信息。
    """
    authz.require_admin(request)
    return meta_db.audit_stats()


@router.post("/audit/seal")
def audit_seal(request: Request) -> Dict[str, Any]:
    """手动封一个链锚（管理员 / 运维例行操作）。

    建议例行化：每日收盘、每次系统升级前后各封一次 ——
    锚越密，"某段时间的记录被整体替换"越难成立。
    """
    p = authz.require_admin(request)
    from app.core import audit as audit_store
    rec = audit_store.seal(reason=f"manual:{p.get('user_id')}")
    meta_db.log_auth_event("audit_sealed", p.get("user_id"), settings.auth_mode, "admin",
                           f"封锚 seq={rec['seq']} count={rec['count']}",
                           identity.client_ip(request))
    return {"ok": True, "anchor": rec}


@router.get("/source/health")
def source_health(request: Request) -> Dict[str, Any]:
    """数据源只读体检：确认问数链路对源库零写权限。（会真的发一条写语句去撞守门，故限管理员）"""
    authz.require_admin(request)
    try:
        info = his.ping()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # 只读自检：尝试一条写语句，必须被守门拦住
    try:
        his.query("DELETE FROM MEDREC.PAT_VISIT")
        guard = "❌ 守门失效：写语句被执行"
    except Exception as e:
        guard = f"✅ 只读守门生效（{type(e).__name__}）"
    return {**info, "readonly_guard": guard}
