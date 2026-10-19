# -*- coding: utf-8 -*-
"""身份解析与授权判定 —— **服务端**的唯一入口（docs/12 §3.4 / §6.3）。

职责边界（v0.18 起拆开）：
    `app/core/identity.py`  负责「你是谁」—— demo / gateway / sso 三种来源
    `app/core/session.py`   负责 sso 模式下的服务端会话
    本模块                  负责「你能干什么」—— 把主体送进权限判定，或直接拒绝

为什么权限判断必须集中在一处：
    此前每个路由各自读 `user_id` 参数，于是"谁能看别人的审计/快照/改数据源"
    完全取决于调用方传了什么 —— 前端门禁只是好看的锁。

授权语义：
    - `require_admin`：管理端/写操作。`admin` 只代表"能看别人在问什么、
      能改配置"，**不放大任何数据范围**（12 §3.4）。
    - `require_self_or_admin`：快照、会话、审计这类"我的历史"。
      越权一律按 404 处理（403 等于承认"这条数据存在"）。

自测：`python -m app.core.authz --selftest`
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException, Request

from app.config import settings
from app.core import identity, permission

# 兼容旧引用：可信头名单现在由 identity 维护（那里还多了一层来源白名单校验）
TRUSTED_HEADERS = identity.TRUSTED_HEADERS
USER_FIELD = identity.USER_FIELD


def identity_note(p: Dict[str, Any]) -> str:
    """一句话说明这个身份是怎么来的 —— 界面与报错都要照实显示。"""
    return identity.identity_note(p)


def principal(request: Optional[Request] = None,
              user_id: Optional[str] = None) -> Dict[str, Any]:
    """解析调用主体。拿不到可信身份就**拒绝**（401），不回落成匿名或默认用户。

    注意：`gateway`/`sso` 模式下 `user_id` 参数被忽略（12 §2 红线 4）。
    保留这个形参只是为了不改动既有路由签名 —— 它的语义是"demo 模式的演示身份"。
    """
    r = identity.resolve(request, user_id)
    if r.get("ok"):
        return r["user"]
    if r.get("reason") == "login_required":
        lu = identity.login_url(request)
        hint = (f"登录地址：{lu['url']}" if lu.get("ok")
                else f"SSO 对接参数未配置，缺：{', '.join(lu.get('missing') or [])}"
                     f"（docs/12 §3.5 对接清单）")
        raise HTTPException(status_code=401, detail=f"{r.get('note')}；{hint}",
                            headers={"X-Login-Required": "1"})
    raise HTTPException(
        status_code=401,
        detail=r.get("note") or f"身份不可信（reason={r.get('reason')}），已拒绝")


def require_admin(request: Optional[Request] = None,
                  user_id: Optional[str] = None) -> Dict[str, Any]:
    """管理端与写操作：必须是 admin 角色。"""
    p = principal(request, user_id)
    if not permission.is_admin(p):
        note = identity_note(p)
        raise HTTPException(
            status_code=403,
            detail=(f"需要管理权限（当前身份 {p.get('user_id')} / {p.get('role')}）"
                    + (f"；{note}" if note else "")))
    return p


def require_self_or_admin(request: Optional[Request] = None,
                          target_user_id: Optional[str] = None,
                          user_id: Optional[str] = None) -> Dict[str, Any]:
    """「我的历史」类数据：本人或管理员。

    非管理员且要读别人的 → 404（**不是 403**）：403 会告诉对方"这份数据存在"。
    """
    p = principal(request, user_id)
    if permission.is_admin(p):
        return p
    if target_user_id and target_user_id != p.get("user_id"):
        raise HTTPException(status_code=404, detail="不存在")
    return p


# ────────────────────────────────────────────────────────────
# 自测：身份来源与授权边界（不需要起服务，全是纯函数）
# ────────────────────────────────────────────────────────────
class _FakeRequest:
    """最小 Request 替身（只用到 client / headers / query_params / cookies）。"""

    def __init__(self, headers: Optional[Dict[str, str]] = None,
                 query: Optional[Dict[str, str]] = None,
                 cookies: Optional[Dict[str, str]] = None,
                 ip: Optional[str] = "127.0.0.1") -> None:
        self.headers = headers or {}
        self.query_params = query or {}
        self.cookies = cookies or {}
        self.client = type("C", (), {"host": ip})() if ip else None


def selftest() -> bool:
    """运行：python -m app.core.authz --selftest"""
    import tempfile
    import time
    from pathlib import Path

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    def status_of(fn) -> int:
        try:
            fn()
            return 200
        except HTTPException as e:
            return int(e.status_code)

    print("身份与授权自测（docs/12 §3.4 / §6.3 / §7 A 组）")
    from app.core import session

    old = (settings.auth_mode, settings.trusted_proxies, settings.meta_db_path)
    tmp = Path(tempfile.gettempdir()) / f"ask_authz_selftest_{int(time.time() * 1000)}.db"
    settings.meta_db_path = str(tmp)
    try:
        # ① demo：能切换身份，但**必须**标为不可信；管理操作照样要判角色
        settings.auth_mode = "demo"
        p = principal(_FakeRequest(), None)
        check("demo 无参 → guest（最小权限，不是某个真实角色）",
              p["user_id"] == "guest" and not p["identity_trusted"], p["user_id"])
        p5 = principal(_FakeRequest(query={"user_id": "u005"}), None)
        check("demo 下可切换身份（演示的定义），但标注不可信",
              p5["user_id"] == "u005" and p5["identity_trusted"] is False)
        check("demo：非管理员被挡在管理操作外",
              status_of(lambda: require_admin(_FakeRequest(query={"user_id": "u001"}))) == 403)
        check("demo：管理员放行",
              status_of(lambda: require_admin(_FakeRequest(query={"user_id": "u005"}))) == 200)

        # ② gateway：只认可信来源 + 可信头，参数一律忽略
        settings.auth_mode = "gateway"
        settings.trusted_proxies = []
        check("gateway：未配置可信代理名单 → 401（没有白名单就无法区分网关注入与伪造）",
              status_of(lambda: principal(_FakeRequest(headers={"x-auth-user": "u005"},
                                                       ip="10.1.33.9"), None)) == 401)
        settings.trusted_proxies = ["10.1.33.0/24"]
        check("gateway：来源不在白名单 → 401（**伪造头**换不到身份）",
              status_of(lambda: principal(_FakeRequest(headers={"x-auth-user": "u005"},
                                                       ip="10.1.34.9"), None)) == 401)
        check("gateway：没有可信头 → 401（fail-closed，不猜身份）",
              status_of(lambda: principal(_FakeRequest(ip="10.1.33.9"), None)) == 401)
        check("gateway：**参数里的 user_id 不能当身份**（否则网关注入可被查询参数绕过）",
              status_of(lambda: principal(_FakeRequest(query={"user_id": "u005"},
                                                       ip="10.1.33.9"), None)) == 401)
        pg = principal(_FakeRequest(headers={"x-auth-user": "u005"},
                                    query={"user_id": "u001"}, ip="10.1.33.9"), None)
        check("gateway：认头，且忽略被伪造的参数",
              pg["user_id"] == "u005" and pg["identity_trusted"] is True
              and pg["identity_header"] == "x-auth-user",
              f"{pg['user_id']} / {pg['identity_header']}")
        check("gateway：管理员由头里的身份判定",
              status_of(lambda: require_admin(_FakeRequest(headers={"x-auth-user": "u005"},
                                                           ip="10.1.33.9"))) == 200)
        check("gateway：普通人被挡",
              status_of(lambda: require_admin(_FakeRequest(headers={"x-auth-user": "u001"},
                                                           ip="10.1.33.9"))) == 403)

        # ③ sso：认服务端会话；无会话 → 401 且**告诉前端去哪登录**
        settings.auth_mode = "sso"
        settings.trusted_proxies = ["10.1.33.0/24"]
        check("sso：无会话 → 401（A1：不得回落到任何默认用户）",
              status_of(lambda: principal(_FakeRequest(ip="10.1.33.9"), None)) == 401)
        try:
            principal(_FakeRequest(ip="10.1.33.9"), None)
        except HTTPException as e:
            check("sso：401 里带登录指引（只说「请登录」对客户端没用）",
                  "登录" in str(e.detail), str(e.detail)[:70])
            check("sso：401 带 X-Login-Required 头（前端据此跳登录，不必猜状态码）",
                  (e.headers or {}).get("X-Login-Required") == "1")
        s = session.create("u005", "信息科", auth_mode="sso", auth_source="cas",
                           client_ip="10.1.33.9")
        req = _FakeRequest(cookies={settings.session_cookie_name: s["token"]},
                           query={"user_id": "u001"}, ip="10.1.33.9")
        ps = principal(req, None)
        check("sso：有会话 → 身份取自会话，参数里的 u001 被忽略",
              ps["user_id"] == "u005" and ps["identity_source"] == "sso")
        check("sso：管理员会话可进管理端", status_of(lambda: require_admin(req)) == 200)
        session.revoke(s["token"], "logout")
        check("sso：登出后立即 401（不等会话过期）",
              status_of(lambda: principal(req, None)) == 401)

        # ④ 「我的历史」：本人或管理员，越权按 404
        settings.auth_mode = "demo"
        check("读别人的历史 → 404（不是 403，403 等于承认它存在）",
              status_of(lambda: require_self_or_admin(
                  _FakeRequest(query={"user_id": "u001"}), "u002")) == 404)
        check("读自己的历史 → 放行",
              status_of(lambda: require_self_or_admin(
                  _FakeRequest(query={"user_id": "u001"}), "u001")) == 200)
        check("管理员可读任何人的历史",
              status_of(lambda: require_self_or_admin(
                  _FakeRequest(query={"user_id": "u005"}), "u001")) == 200)

        # ⑤ 身份说明转发（界面靠它区分"演示"与"已鉴权"）
        check("identity_note：gateway 说明里含来源 IP 与头名",
              "10.1.33.9" in identity_note({"identity_source": "gateway",
                                            "identity_header": "x-auth-user",
                                            "client_ip": "10.1.33.9"}))
    finally:
        settings.auth_mode = old[0]
        settings.trusted_proxies = old[1]
        settings.meta_db_path = old[2]
        try:
            tmp.unlink()
        except OSError:
            pass

    print("=" * 60)
    print("自测结果: " + ("全部通过" if passed else "存在失败"))
    return passed


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
