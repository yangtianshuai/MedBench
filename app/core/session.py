# -*- coding: utf-8 -*-
"""服务端会话（F6.1 · docs/12 §3.3）。

为什么必须有这一层：
    `sso` 模式下身份来自**一次性票据**（CAS ticket / OIDC code），票据用过即废。
    此后每个请求认的只能是服务端会话 —— 客户端手里没有任何可用于自证的东西。

三条不可回退的约束：
  1) Cookie 里**只放一个不透明随机令牌**，不放工号/角色/科室。
     放工号就等于把"我是谁"交回客户端；放角色就等于让用户自己改权限。
  2) 登出必须**服务端立即失效**，不能只清 Cookie ——
     清 Cookie 只是"请用户忘掉它"，旧值照样能用。
     转岗/离职用 `revoke_user()` 一键踢掉该账号全部会话。
  3) 过期与空闲超时**全部由服务端判定**。客户端带来的时间戳不参与判定，
     否则改一下 Cookie 就能永久在线。

自测：`python -m app.core.session --selftest`（用临时库，不碰真实数据）
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Dict, Optional, Tuple

from app.config import settings
from app.core import meta_db

# 令牌长度：32 字节 ≈ 43 个 URL-safe 字符（256 bit 熵，不可枚举）
TOKEN_BYTES = 32

# 距上次访问不足这个秒数就不写库：会话续期是**每请求一次写**，
# 不设下限会让问数首屏的每次轮询都产生一次磁盘写。
TOUCH_INTERVAL = 60.0

# 会话行的清理宽限期：撤销/过期后保留一段时间供追溯（谁在什么时候被踢的）。
GRACE_SECONDS = 7 * 24 * 3600


def new_token() -> str:
    """不透明随机令牌。**不含**任何身份信息（不做"签名身份证"那种设计）。"""
    return secrets.token_urlsafe(TOKEN_BYTES)


def ttl_for(is_admin: bool = False) -> Tuple[int, int]:
    """(绝对过期秒数, 空闲过期秒数)。

    管理端空闲阈值更短：能改数据源与口径的会话被劫持，后果与"看数"不是一个量级。
    """
    absolute = max(1, settings.session_ttl_hours) * 3600
    idle = (max(1, settings.admin_session_idle_minutes) if is_admin
            else max(1, settings.session_idle_minutes)) * 60
    # 空闲阈值不得超过绝对上限，否则"空闲过期"永远不会先触发（自测会指出这点）
    return absolute, min(idle, absolute)


def create(user_id: str, role: Optional[str] = None, *,
           auth_mode: Optional[str] = None, auth_source: Optional[str] = None,
           client_ip: Optional[str] = None, user_agent: Optional[str] = None,
           is_admin: bool = False, now: Optional[float] = None) -> Dict[str, Any]:
    """建立服务端会话，返回令牌与过期时刻（**令牌只在这里出现一次**）。"""
    t0 = time.time() if now is None else now
    absolute, idle = ttl_for(is_admin)
    token = new_token()
    row = {
        "token": token, "user_id": user_id, "role": role,
        "auth_mode": auth_mode or settings.auth_mode,
        "auth_source": auth_source,
        "created_at": t0, "last_seen": t0,
        "expires_at": t0 + absolute, "idle_expires_at": t0 + idle,
        "client_ip": client_ip, "user_agent": (user_agent or "")[:200],
    }
    meta_db.insert_user_session(row)
    return {"token": token, "user_id": user_id, "created_at": row["created_at"],
            "expires_at": row["expires_at"], "idle_expires_at": row["idle_expires_at"],
            "absolute_seconds": absolute, "idle_seconds": idle}


def resolve(token: Optional[str], *, now: Optional[float] = None,
            touch: bool = True) -> Dict[str, Any]:
    """把 Cookie 里的令牌解成主体。**任何一步不成立都返回 ok=False**，不猜。

    返回 `{"ok": bool, "reason": str, ...}`；reason 取值：
      `missing` 未带令牌 / `not_found` 令牌不存在（伪造或已被清理）/
      `revoked` 已登出或被踢 / `expired` 绝对超时 / `idle_expired` 空闲超时。
    """
    if not token:
        return {"ok": False, "reason": "missing"}
    row = meta_db.get_user_session(token)
    if not row:
        return {"ok": False, "reason": "not_found"}

    t = time.time() if now is None else now
    if row.get("revoked"):
        return {"ok": False, "reason": "revoked", "user_id": row.get("user_id"),
                "revoked_reason": row.get("revoked_reason")}
    # ★ 先判绝对过期、再判空闲：顺序影响 reason 的可读性（两者都超时时应说"到点了"，
    #   而不是"你半小时没动" —— 后者会让运维去查用户行为，方向是错的）。
    if t >= float(row["expires_at"]):
        meta_db.revoke_user_session(token, "expired")
        return {"ok": False, "reason": "expired", "user_id": row.get("user_id")}
    if t >= float(row["idle_expires_at"]):
        meta_db.revoke_user_session(token, "idle_expired")
        return {"ok": False, "reason": "idle_expired", "user_id": row.get("user_id")}

    if touch and t - float(row["last_seen"]) >= TOUCH_INTERVAL:
        # 续期但不越过绝对上限：空闲阈值可以续，绝对上限不给续
        meta_db.touch_user_session(token, t, min(t + (float(row["idle_expires_at"]) -
                                                 float(row["last_seen"])),
                                            float(row["expires_at"])))
    return {"ok": True, "reason": "", "user_id": row["user_id"], "role": row.get("role"),
            "auth_mode": row.get("auth_mode"), "auth_source": row.get("auth_source"),
            "created_at": row.get("created_at"), "last_seen": row.get("last_seen"),
            "expires_at": row.get("expires_at"), "idle_expires_at": row.get("idle_expires_at"),
            "client_ip": row.get("client_ip")}


def revoke(token: Optional[str], reason: str = "logout") -> bool:
    """登出：服务端立即失效。返回是否真的撤销了一条（已撤销/不存在 → False）。"""
    if not token:
        return False
    return meta_db.revoke_user_session(token, reason)


def revoke_user(user_id: str, reason: str = "revoked",
                except_token: Optional[str] = None) -> int:
    """踢掉某账号的全部会话（转岗 / 离职 / 异常访问，docs/12 §7 B5）。"""
    return meta_db.revoke_all_user_sessions(user_id, reason, except_token)


def purge_expired(now: Optional[float] = None) -> int:
    return meta_db.purge_expired_user_sessions(time.time() if now is None else now)


# ── Cookie（唯一与传输层打交道的地方）──
def set_cookie(response, token: str, max_age: int) -> None:
    """写会话 Cookie。

    HttpOnly：脚本读不到（XSS 拿不走会话）；
    SameSite=Lax：CAS/OIDC 回调是顶层 GET 跳转，Lax 能带上 Cookie，
                  同时又挡住跨站表单提交与跨站子请求 —— 兼顾可用与 CSRF 防护；
    Secure    ：内网 HTTPS 由院方确认后打开（`ASK_SESSION_COOKIE_SECURE=1`）。
    """
    response.set_cookie(
        key=settings.session_cookie_name, value=token, max_age=int(max_age),
        httponly=True, samesite="lax", secure=bool(settings.session_cookie_secure),
        path="/")


def clear_cookie(response) -> None:
    """清 Cookie 只是"请用户忘掉"，**同时必须** revoke() 服务端会话才算登出。"""
    response.delete_cookie(key=settings.session_cookie_name, path="/")


def read_token(request) -> Optional[str]:
    try:
        return request.cookies.get(settings.session_cookie_name)
    except Exception:
        return None


# ────────────────────────────────────────────────────────────
# 自测：会话的失效条件必须条条可验证
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    """运行：python -m app.core.session --selftest"""
    import tempfile
    from pathlib import Path

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    print("服务端会话自测（docs/12 §3.3）")
    old_path = settings.meta_db_path
    tmp = Path(tempfile.gettempdir()) / f"ask_session_selftest_{int(time.time() * 1000)}.db"
    settings.meta_db_path = str(tmp)          # 自测绝不碰真实库
    t0 = 1_700_000_000.0
    try:
        # ① 令牌本身：不透明、不可猜、不携带身份
        s = create("u002", "科室主任", auth_source="cas", now=t0)
        tok = s["token"]
        check("令牌是不透明随机串，且**不含**工号",
              tok != "u002" and "u002" not in tok and len(tok) >= 32, f"len={len(tok)}")
        check("两次签发的令牌不同（不可预测）",
              create("u002", "科室主任", now=t0)["token"] != tok)

        # ② 正常解析
        r = resolve(tok, now=t0 + 10)
        check("有效会话可解析出主体", r["ok"] and r["user_id"] == "u002" and r["role"] == "科室主任",
              f"reason={r['reason']}")
        check("Cookie 里只有令牌，没有角色/科室（改 Cookie 改不了权限）",
              "科室主任" not in tok and "0101" not in tok)

        # ③ 未知令牌：伪造 Cookie 得不到任何身份
        check("伪造令牌 → not_found（不猜、不匿名放行）",
              resolve("not-a-real-token", now=t0)["reason"] == "not_found")
        check("空令牌 → missing", resolve(None, now=t0)["reason"] == "missing")

        # ④ 空闲超时（默认 30 分钟）：用户没动过就该失效
        s2 = create("u002", "科室主任", now=t0)
        r2 = resolve(s2["token"], now=t0 + 31 * 60)
        check("空闲 31 分钟 → idle_expired", r2["reason"] == "idle_expired", r2["reason"])

        # ⑤ 活跃用户不该被空闲超时踢：每次访问续期
        s3 = create("u002", "科室主任", now=t0)
        t = t0
        alive = True
        for _ in range(20):                    # 每 20 分钟访问一次，跨度 6.7 小时
            t += 20 * 60
            alive = alive and resolve(s3["token"], now=t, touch=True)["ok"]
        r3 = resolve(s3["token"], now=t + 5 * 60)   # 5 分钟后再来，仍在空闲窗口内
        check("持续活跃 6.7 小时后仍然有效（续期生效）", alive and r3["ok"],
              f"alive={alive} reason={r3['reason']}")

        # ⑥ 绝对过期（8 小时）：再活跃也不能无限续
        s4 = create("u002", "科室主任", now=t0)
        t = t0
        for _ in range(20):
            t += 20 * 60                       # 只走到 6.7 小时，不超过绝对上限
            resolve(s4["token"], now=t, touch=True)
        r4 = resolve(s4["token"], now=t0 + 9 * 3600, touch=False)
        check("绝对 9 小时后失效（空闲续期不能突破绝对上限）",
              r4["reason"] == "expired", r4["reason"])

        # ⑦ 登出：服务端立即失效（这一条是"只清 Cookie"与"真登出"的分界）
        s5 = create("u002", "科室主任", now=t0)
        check("登出前有效", resolve(s5["token"], now=t0 + 5)["ok"])
        revoked = revoke(s5["token"], "logout")
        check("登出返回 True（确实撤销了一条）", revoked is True)
        check("登出后同一令牌立即失效（不等到过期）",
              resolve(s5["token"], now=t0 + 6)["reason"] == "revoked")
        check("重复登出返回 False（如实回显，不假装成功）", revoke(s5["token"]) is False)

        # ⑧ 踢账号：转岗/离职时一键下线，但保留当前这台设备
        a = create("u004", "财务处", now=t0)
        b = create("u004", "财务处", now=t0)
        n = revoke_user("u004", "offboard", except_token=b["token"])
        check("踢账号：撤销其它设备、保留当前会话",
              n == 1 and resolve(a["token"], now=t0 + 1)["reason"] == "revoked"
              and resolve(b["token"], now=t0 + 1)["ok"])

        # ⑨ 管理端空闲阈值更短
        _, idle_user = ttl_for(False)
        _, idle_admin = ttl_for(True)
        check(f"管理端空闲 {idle_admin // 60} 分钟 < 普通 {idle_user // 60} 分钟",
              idle_admin < idle_user)
        abs_s, idle_s = ttl_for(True)
        check("空闲阈值不超过绝对上限", idle_s <= abs_s)

        # ⑩ Cookie 属性：HttpOnly + SameSite=Lax + 只放令牌
        class _Resp:
            def __init__(self): self.kwargs = None
            def set_cookie(self, **kw): self.kwargs = kw
        resp = _Resp()
        set_cookie(resp, tok, 3600)
        kw = resp.kwargs or {}
        check("Cookie: HttpOnly（脚本读不到）", kw.get("httponly") is True)
        check("Cookie: SameSite=Lax（回调能带上，跨站提交挡掉）", kw.get("samesite") == "lax")
        check("Cookie: 值是令牌本身，不含身份字段", kw.get("value") == tok)

        # ⑪ 清理只清"早就没用了"的行（撤销记录本身是证据）
        fresh = create("u001", "院长", now=t0)
        revoke(fresh["token"], "logout")
        purged = purge_expired(t0 + 1)          # 刚撤销、未过 7 天宽限
        check("宽限期内不删撤销记录（谁被踢掉要留痕）", purged == 0 and
              meta_db.get_user_session(fresh["token"]) is not None)
        meta_db.purge_user_session(fresh["token"])

        # ⑫ 身份事件留痕（§4.4）：与审计分开，只追加
        meta_db.log_auth_event("logout", "u002", "sso", "cas", "用户主动登出", "10.1.33.9")
        evs = meta_db.list_auth_events(kind="logout", user_id="u002")
        check("身份事件可查（登录/登出/票据失败走这张表，不污染问数审计）",
              len(evs) == 1 and evs[0]["source"] == "cas")
    finally:
        settings.meta_db_path = old_path
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
