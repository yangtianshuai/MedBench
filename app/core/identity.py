# -*- coding: utf-8 -*-
"""身份授信（F6.1 · docs/12 §3）—— 服务端**唯一**能决定「你是谁」的地方。

三种来源（`ASK_AUTH_MODE`），语义完全不同，不许互相混用：

| 模式 | 身份从哪来 | 可否上线 |
|---|---|---|
| `demo` | 请求参数里的 `user_id`（谁都能切换） | ❌ 只允许本机演示（security_gate 强校验） |
| `gateway` | 网关注入的可信头，且**来源必须在白名单内** | ✅ 生产 |
| `sso` | CAS / OIDC 票据换来的**服务端会话** | ✅ 生产 |

两条贯穿全模块的判定原则：

1. **fail-closed**：拿不到可信身份就拒绝，绝不"回落成匿名/默认用户"。
   回落是最危险的形态 —— 系统照常出数，只是出给了不该看的人。
2. **不认客户端自报**：`gateway`/`sso` 模式下请求参数里的 `user_id` 一律忽略
   （12 §2 红线 4）。这条之所以要写死在代码里，是因为"顺手支持一下传参"太容易了。

授权每次都重新解析（`permission.resolve_user`），会话里**只存工号** ——
这样转岗/离职在院内系统改一次即生效，不必等会话过期（12 §4.2）。

自测：`python -m app.core.identity --selftest`（含 CAS / OIDC 的 mock 服务端）
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.core import meta_db, permission, session

# 网关注入主体时常用的头名（Nginx ingress / OAuth2 proxy / Kerberos 各有习惯）
TRUSTED_HEADERS = ("x-auth-user", "x-forwarded-user", "x-remote-user", "remote-user")
USER_FIELD = "x-auth-user-field"          # 可选：头里放的是工号字段名而非工号本身

VALID_MODES = ("demo", "gateway", "sso")
STATE_TTL = 600                            # OIDC state 有效期（秒）

# 可注入的 HTTP 客户端（自测用它替换掉真实网络调用）
http_get = None                            # type: ignore[assignment]
http_post_form = None                      # type: ignore[assignment]
_FALLBACK_KEY: Optional[bytes] = None


# ────────────────────────────────────────────────────────────
# 小工具
# ────────────────────────────────────────────────────────────
def client_ip(request) -> Optional[str]:
    try:
        return request.client.host if request.client else None
    except Exception:
        return None


def _norm_ip(ip: Optional[str]) -> Optional[str]:
    """把 IPv6 形式的 IPv4（::ffff:10.1.33.9）归一，否则白名单永远匹配不上。"""
    if not ip:
        return None
    s = ip.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    if s.count(":") == 1 and s.rsplit(":", 1)[1].isdigit():
        s = s.rsplit(":", 1)[0]            # 10.1.33.9:54321
    try:
        a = ipaddress.ip_address(s)
    except ValueError:
        return s
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        return str(a.ipv4_mapped)
    return str(a)


def ip_allowed(ip: Optional[str], patterns: Optional[List[str]] = None) -> bool:
    """来源是否在可信代理名单内。支持精确 IP 与 CIDR（`10.1.33.0/24`）。

    ★ 认不出来的一律**不允许**：这是安全判定，fail-closed 的方向是"更严"。
    """
    pats = settings.trusted_proxies if patterns is None else patterns
    if not pats:
        return False
    v = _norm_ip(ip)
    if not v:
        return False
    try:
        addr = ipaddress.ip_address(v)
    except ValueError:
        return False
    for p in pats:
        p = (p or "").strip()
        if not p:
            continue
        try:
            if "/" in p:
                if addr in ipaddress.ip_network(p, strict=False):
                    return True
            elif addr == ipaddress.ip_address(p):
                return True
        except ValueError:
            continue                        # 写错的条目跳过，不影响其它条目
    return False


def _attr(attrs: Dict[str, Any], name: str) -> Optional[str]:
    """从属性表取一个值。CAS/OIDC 的属性值可能是标量或单元素列表。"""
    if not name:
        return None
    v = attrs.get(name)
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if v is None:
        return None
    return str(v).strip() or None


def _service_url(request=None) -> str:
    """本服务的回调地址。必须与 CAS/OIDC 侧登记的**完全一致**，否则票据必校验失败。"""
    if settings.sso_service_url:
        return settings.sso_service_url
    if request is not None:
        try:
            return str(request.base_url).rstrip("/")
        except Exception:
            return ""
    return ""


# ────────────────────────────────────────────────────────────
# SSO：CAS / OIDC
# ────────────────────────────────────────────────────────────
def _ssl_ctx():
    if settings.sso_verify_tls:
        return ssl.create_default_context()
    # 内部自签证书时才会走到这里；默认是校验的（关掉校验等于把票据交给中间人）
    return ssl._create_unverified_context()          # noqa: SLF001


def _default_http_get(url: str) -> Tuple[int, str]:
    req = urllib.request.Request(url, headers={"Accept": "application/json, text/xml, */*"})
    try:
        with urllib.request.urlopen(req, timeout=settings.sso_timeout,
                                    context=_ssl_ctx()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:               # 4xx/5xx 也要把正文带回来解析
        return e.code, e.read().decode("utf-8", "replace")


def _default_http_post_form(url: str, data: Dict[str, str]) -> Tuple[int, str]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=settings.sso_timeout,
                                    context=_ssl_ctx()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _get(url: str) -> Tuple[int, str]:
    return (http_get or _default_http_get)(url)


def _post_form(url: str, data: Dict[str, str]) -> Tuple[int, str]:
    return (http_post_form or _default_http_post_form)(url, data)


def _xml_user(xml_text: str) -> Optional[str]:
    """从 CAS 的 XML 响应里取 `<cas:user>`（命名空间各家写法不一，按 localname 匹配）。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for el in root.iter():
        if el.tag.split("}")[-1] == "user" and (el.text or "").strip():
            return el.text.strip()
    return None


def _xml_attrs(xml_text: str) -> Dict[str, str]:
    """取 `<cas:attributes>` 下的子元素（属性名去掉命名空间）。"""
    out: Dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for el in root.iter():
        if el.tag.split("}")[-1] == "attributes":
            for ch in el:
                name = ch.tag.split("}")[-1]
                if (ch.text or "").strip():
                    out[name] = ch.text.strip()
    return out


def cas_validate(ticket: str, service: str) -> Dict[str, Any]:
    """校验 CAS 票据（CAS 2.0 / 3.0）。

    先请求 JSON（CAS 3.0 支持 `format=JSON`），失败再按 XML 解析 ——
    11g 时代的老 CAS 服务端只给 XML，不能只认一种。
    """
    if not settings.cas_base_url:
        return {"ok": False, "error": "未配置 ASK_CAS_BASE（院方统一身份平台地址）"}
    if not service:
        return {"ok": False, "error": "未配置 ASK_SSO_SERVICE_URL，且无法从请求推导回调地址"}
    base = settings.cas_base_url + settings.cas_service_validate_path
    q = urllib.parse.urlencode({"service": service, "ticket": ticket, "format": "JSON"})
    status, text = _get(f"{base}?{q}")
    uid: Optional[str] = None
    attrs: Dict[str, str] = {}
    # ★ 区分两件事：**票据被拒**（authenticationFailure）与
    #   **票据通过了但里面没有工号**。混成一条报错会让院方对接时查错方向
    #   （前者查票据/时钟/回调地址，后者查属性名映射，完全是两拨人）。
    success = False
    try:
        j = json.loads(text)
        sr = (j or {}).get("serviceResponse") or {}
        ok_node = sr.get("authenticationSuccess")
        if ok_node:
            success = True
            uid = str(ok_node.get("user") or "").strip() or None
            a = ok_node.get("attributes") or {}
            attrs = {k: (v[0] if isinstance(v, list) and v else v) for k, v in a.items()}
            attrs = {k: str(v) for k, v in attrs.items() if v is not None}
    except (ValueError, AttributeError):
        if "<cas:authenticationSuccess" in text or "authenticationSuccess" in text:
            success = True
            uid = _xml_user(text)
            attrs = _xml_attrs(text)
    if not success:
        return {"ok": False, "error": f"票据校验失败或已过期（HTTP {status}）：{text[:200]}"}
    return {"ok": True, "user_id": uid, "attrs": attrs}


def oidc_exchange(code: str, redirect_uri: str) -> Dict[str, Any]:
    """OIDC 授权码换主体：code → token → userinfo。"""
    if not settings.oidc_issuer or not settings.oidc_client_id:
        return {"ok": False, "error": "未配置 ASK_OIDC_ISSUER / ASK_OIDC_CLIENT_ID"}
    status, text = _post_form(f"{settings.oidc_issuer}/token", {
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": redirect_uri, "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret})
    try:
        tok = json.loads(text)
    except ValueError:
        return {"ok": False, "error": f"换取令牌失败（HTTP {status}）：{text[:200]}"}
    at = tok.get("access_token")
    if not at:
        return {"ok": False, "error": f"未取得 access_token：{text[:200]}"}
    status2, text2 = _get(f"{settings.oidc_issuer}/userinfo?"
                          + urllib.parse.urlencode({"access_token": at}))
    try:
        info = json.loads(text2)
    except ValueError:
        return {"ok": False, "error": f"userinfo 解析失败（HTTP {status2}）：{text2[:200]}"}
    return {"ok": True, "attrs": info}


def _state_key() -> bytes:
    """OIDC state 的签名密钥。

    生产建议设 `ASK_SESSION_SECRET`（多实例必须一致，否则 A 实例发的 state
    到 B 实例校验不过）。没设时用**进程内随机**：重启即失效，
    只影响"登录页开着的时候重启了服务"这一种情况，不涉及身份可信度。
    """
    import os
    seed = os.getenv("ASK_SESSION_SECRET") or ""
    if seed:
        return hashlib.sha256(seed.encode()).digest()
    global _FALLBACK_KEY
    if _FALLBACK_KEY is None:
        _FALLBACK_KEY = secrets.token_bytes(32)
    return _FALLBACK_KEY


def make_state() -> str:
    ts = str(int(time.time()))
    sig = hmac.new(_state_key(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"


def check_state(state: Optional[str]) -> bool:
    """校验 OIDC 回调的 state：防重放 + 防跨站伪造回调。"""
    if not state or "." not in state:
        return False
    ts, sig = state.split(".", 1)
    if not ts.isdigit() or abs(time.time() - int(ts)) > STATE_TTL:
        return False
    expect = hmac.new(_state_key(), ts.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expect)


def login_url(request=None) -> Dict[str, Any]:
    """登录跳转地址。**配置缺失时如实返回缺什么**，不生成一个必然失败的 URL。"""
    proto = (settings.sso_protocol or "cas").lower()
    service = _service_url(request)
    if proto == "cas":
        missing = []
        if not settings.cas_base_url:
            missing.append("ASK_CAS_BASE")
        if not service:
            missing.append("ASK_SSO_SERVICE_URL")
        if missing:
            return {"ok": False, "url": None, "protocol": "cas",
                    "missing": missing, "note": "SSO 对接参数未配置（docs/12 §3.5 对接清单）"}
        cb = f"{service}/api/security/callback"
        return {"ok": True, "protocol": "cas", "url":
                f"{settings.cas_base_url}/login?" + urllib.parse.urlencode({"service": cb}),
                "missing": [], "note": ""}
    # OIDC
    missing = []
    if not settings.oidc_issuer:
        missing.append("ASK_OIDC_ISSUER")
    if not settings.oidc_client_id:
        missing.append("ASK_OIDC_CLIENT_ID")
    if not service:
        missing.append("ASK_SSO_SERVICE_URL")
    if missing:
        return {"ok": False, "url": None, "protocol": "oidc",
                "missing": missing, "note": "SSO 对接参数未配置（docs/12 §3.5 对接清单）"}
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": settings.oidc_client_id,
        "redirect_uri": f"{service}/api/security/callback",
        "scope": settings.oidc_scopes, "state": make_state()})
    return {"ok": True, "protocol": "oidc", "url": f"{settings.oidc_issuer}/authorize?{q}",
            "missing": [], "note": ""}


def handle_callback(request) -> Dict[str, Any]:
    """处理 SSO 回调：校验票据 → 建服务端会话 → 返回工号。

    属性映射（`ASK_ATTR_USER` 等）由院方身份平台决定 —— 取不到工号一律失败，
    不许"用姓名兜底"（姓名不唯一，且授权表按工号绑）。
    """
    proto = (settings.sso_protocol or "cas").lower()
    qp = request.query_params
    service = _service_url(request)
    if proto == "cas":
        ticket = qp.get("ticket")
        if not ticket:
            return {"ok": False, "error": "回调缺少 ticket"}
        r = cas_validate(ticket, f"{service}/api/security/callback")
        attrs = r.get("attrs") or {}
        # ★ 工号优先取属性（院方平台常把"工号"放在自定义属性里），
        #   属性没有才回退到 <cas:user>。反过来（只认 user）会在
        #   `user` 放的是登录名、工号在属性里的平台上绑错人。
        uid = _attr(attrs, settings.attr_user) or r.get("user_id")
    else:
        if not check_state(qp.get("state")):
            return {"ok": False, "error": "state 校验失败（回调可能不是本次登录发起）"}
        code = qp.get("code")
        if not code:
            return {"ok": False, "error": "回调缺少 code"}
        r = oidc_exchange(code, f"{service}/api/security/callback")
        attrs = r.get("attrs") or {}
        uid = _attr(attrs, settings.attr_user)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "票据校验失败"}
    if not uid:
        return {"ok": False, "error": f"身份平台未返回工号属性（当前取的是 "
                                     f"ASK_ATTR_USER={settings.attr_user}）；"
                                     f"属性名需与院方确认（docs/12 §3.5）"}
    return {"ok": True, "user_id": str(uid), "attrs": attrs,
            "name": _attr(attrs, settings.attr_name),
            "dept_code": _attr(attrs, settings.attr_dept),
            "campus_code": _attr(attrs, settings.attr_campus)}


# ────────────────────────────────────────────────────────────
# 统一入口
# ────────────────────────────────────────────────────────────
def _note_demo() -> str:
    return "演示模式：身份可由任何人切换，**不是安全边界**，禁止上线（docs/12 §3.4）"


def _note_gateway(p: Dict[str, Any]) -> str:
    return f"身份由网关注入（{p.get('identity_header')}，来源 {p.get('client_ip')} 在可信名单内），服务端据此判定授权"


def _note_sso(p: Dict[str, Any]) -> str:
    return (f"身份来自 SSO 票据校验后的服务端会话（{p.get('auth_source')}）；"
            f"会话令牌仅服务端持有，登出即刻失效")


def _note_local() -> str:
    """本地账号只是凭证入口，不是权限来源；界面不再展开这句实现说明。"""
    return ""


def local_login_enabled() -> bool:
    """本地登录入口是否开启（`off` 之外都算开）。"""
    return (settings.auth_local or "off").strip().lower() in ("primary", "breakglass")


def resolve(request=None, user_id: Optional[str] = None) -> Dict[str, Any]:
    """解析调用主体。返回 `{"ok": bool, "reason": str, "user": {...}, "note": str}`。

    ★ `gateway`/`sso` 模式下**忽略** `user_id` 参数：那正是"客户端自报身份"。
    """
    mode = (settings.auth_mode or "demo").strip().lower()
    if mode not in VALID_MODES:
        return {"ok": False, "reason": "bad_mode", "user": None,
                "note": f"ASK_AUTH_MODE={mode} 不是有效值（{'/'.join(VALID_MODES)}）"}
    if mode == "demo":
        uid = user_id
        if not uid and request is not None:
            try:
                uid = request.query_params.get("user_id")
            except Exception:
                uid = None
        p = permission.resolve_user(uid or "guest")
        return {"ok": True, "reason": "", "mode": "demo",
                "user": {**p, "identity_source": "demo", "identity_trusted": False,
                         "identity_header": None, "client_ip": client_ip(request)},
                "note": _note_demo()}
    if mode == "gateway":
        return _resolve_gateway(request)
    # ★ sso 与本地账号**共用**同一条服务端会话链路（docs/12 §3.6）：
    #   会话里只有工号与来源，身份可信度来自"令牌由服务端签发"，
    #   与"当初用票据还是口令换的"无关。分成两条链路写，早晚会出现
    #   「SSO 会话 8 小时、本地会话永不过期」这种不对称。
    return _resolve_session(request)


def _resolve_gateway(request) -> Dict[str, Any]:
    ip = client_ip(request)
    if not settings.trusted_proxies:
        meta_db.log_auth_event("header_rejected", None, "gateway", "gateway",
                               "未配置 ASK_TRUSTED_PROXIES（无白名单 → 拒绝所有可信头）", ip)
        return {"ok": False, "reason": "no_trusted_proxies", "user": None,
                "note": "gateway 模式未配置可信代理名单（ASK_TRUSTED_PROXIES）——"
                        "没有白名单时无法区分「网关注入」与「客户端伪造」，故一律拒绝。"
                        "见 docs/12 §3.4"}
    header_name = header_val = None
    try:
        for h in TRUSTED_HEADERS:
            v = request.headers.get(h)
            if v:
                header_name, header_val = h, v
                break
    except Exception:
        pass
    if not ip_allowed(ip):
        # ★ 关键：来源不在白名单时，即使头存在也**不当身份**。
        #   否则任何能连到本服务的人，加一个 `X-Auth-User: u001` 就是院长。
        meta_db.log_auth_event("header_rejected", header_val, "gateway", "gateway",
                               f"来源 {ip} 不在可信代理名单内（头 {header_name or '-'} 被拒绝）", ip)
        return {"ok": False, "reason": "untrusted_source", "user": None,
                "note": f"来源 {ip} 不在可信代理名单内（ASK_TRUSTED_PROXIES）；"
                        f"网关注入的可信头只在这份名单内的来源上生效（docs/12 §3.4）"}
    if not header_val:
        return {"ok": False, "reason": "no_identity_header", "user": None,
                "note": "生产模式需要网关注入可信身份头（" + " / ".join(TRUSTED_HEADERS) + "）；"
                        "不接受客户端自报 user_id。身份桥接线见 docs/12 §3.5"}
    p = permission.resolve_user(header_val)
    return {"ok": True, "reason": "", "mode": "gateway",
            "user": {**p, "identity_source": "gateway", "identity_trusted": True,
                     "identity_header": header_name, "client_ip": ip},
            "note": _note_gateway({"identity_header": header_name, "client_ip": ip})}


def _resolve_session(request) -> Dict[str, Any]:
    """从**服务端会话**解析主体：SSO 票据与本地口令都走这里。

    唯一的差别是会话行里的 `auth_source`（`cas` / `oidc` / `local`）——
    它决定身份说明的措辞与审计里的来源标记，不决定信任级别。
    """
    token = session.read_token(request) if request is not None else None
    s = session.resolve(token)
    is_local = (s.get("auth_source") or "") == "local"
    if not s.get("ok"):
        if s.get("reason") in ("revoked", "expired", "idle_expired"):
            meta_db.log_auth_event("session_expired", s.get("user_id"),
                                   "local" if is_local else "sso",
                                   "session", s.get("reason"), client_ip(request))
        return {"ok": False, "reason": "login_required", "user": None,
                "session_reason": s.get("reason"),
                "note": ("需要登录：本地账号模式凭服务端会话判定身份"
                         "（登录入口见 /api/security/auth/local/status，docs/12 §3.6）"
                         if local_login_enabled() else
                         "需要登录：SSO 模式下每个请求都凭服务端会话判定身份（docs/12 §3.3）")}
    # ★ 授权**每次都重新解析**，不读会话里缓存的角色：
    #   转岗/离职在院内系统改一次即生效，不必等 8 小时会话过期（12 §4.2）。
    p = permission.resolve_user(s["user_id"])
    src = "local" if is_local else "sso"
    return {"ok": True, "reason": "", "mode": src,
            "user": {**p, "identity_source": src, "identity_trusted": True,
                     "identity_header": None, "client_ip": client_ip(request),
                     "auth_source": s.get("auth_source")},
            "note": (_note_local() if is_local
                     else _note_sso({"auth_source": s.get("auth_source")}))}


# 兼容旧名：语义完全一致（本地账号与 SSO 共用这条链路）
_resolve_sso = _resolve_session


def identity_note(p: Dict[str, Any]) -> str:
    """一句话说明身份是怎么来的 —— 界面与报错都要照实显示。"""
    src = p.get("identity_source")
    if src == "gateway":
        return _note_gateway(p)
    if src == "sso":
        return _note_sso(p)
    if src == "local":
        return _note_local()
    return _note_demo()


# ────────────────────────────────────────────────────────────
# 自测（不需要起服务，也不需要真的 SSO 服务端）
# ────────────────────────────────────────────────────────────
class _FakeRequest:
    """最小 Request 替身（只用到 client / headers / query_params / cookies）。"""

    def __init__(self, headers=None, query=None, cookies=None, ip="127.0.0.1"):
        self.headers = headers or {}
        self.query_params = query or {}
        self.cookies = cookies or {}
        self.client = type("C", (), {"host": ip})() if ip else None


def selftest() -> bool:
    """运行：python -m app.core.identity --selftest"""
    import tempfile
    from pathlib import Path

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    print("身份授信自测（docs/12 §3.1~§3.4 / §7 A 组）")
    old = (settings.meta_db_path, settings.auth_mode, settings.trusted_proxies,
           settings.cas_base_url, settings.sso_service_url, settings.oidc_issuer,
           settings.oidc_client_id, settings.attr_user, settings.sso_protocol)
    tmp = Path(tempfile.gettempdir()) / f"ask_identity_selftest_{int(time.time() * 1000)}.db"
    settings.meta_db_path = str(tmp)
    try:
        # ① IP 白名单：精确 IP、CIDR、以及"看起来像但不是"的地址
        check("白名单：精确 IP 命中", ip_allowed("10.1.33.9", ["10.1.33.9"]))
        check("白名单：CIDR 命中", ip_allowed("10.1.33.9", ["10.1.33.0/24"]))
        check("白名单：CIDR 不越界（10.1.34.9 不在 10.1.33.0/24 内）",
              not ip_allowed("10.1.34.9", ["10.1.33.0/24"]))
        check("白名单：IPv6 形式的 IPv4 归一后能命中",
              ip_allowed("::ffff:10.1.33.9", ["10.1.33.0/24"]))
        check("白名单：空名单 = 谁都不信（fail-closed）", not ip_allowed("10.1.33.9", []))
        check("白名单：写错的条目不影响其它条目",
              ip_allowed("10.1.33.9", ["10.1.33.0/999", "10.1.33.9"]))

        # ② demo：可切换，但如实标注不可信
        settings.auth_mode = "demo"
        settings.trusted_proxies = []
        r = resolve(_FakeRequest(), None)
        check("demo：无参 → guest（最小权限）", r["ok"] and r["user"]["user_id"] == "guest")
        check("demo：标注 identity_trusted=False（界面必须照实显示）",
              r["user"]["identity_trusted"] is False)
        r = resolve(_FakeRequest(query={"user_id": "u005"}), None)
        check("demo：可切换身份（演示的定义）", r["user"]["user_id"] == "u005")

        # ③ gateway：A1/A3 —— 没有可信来源就拒绝，头再像也没用
        settings.auth_mode = "gateway"
        settings.trusted_proxies = []
        r = resolve(_FakeRequest(headers={"x-auth-user": "u001"}, ip="10.1.33.9"), None)
        check("gateway：未配置白名单 → 拒绝（不能靠一个头自证身份）",
              (not r["ok"]) and r["reason"] == "no_trusted_proxies")
        settings.trusted_proxies = ["10.1.33.0/24"]
        r = resolve(_FakeRequest(headers={"x-auth-user": "u001"}, ip="10.1.34.9"), None)
        check("gateway：来源不在白名单 → 拒绝（**伪造头**换不到院长身份）",
              (not r["ok"]) and r["reason"] == "untrusted_source", r["reason"])
        r = resolve(_FakeRequest(headers={"x-auth-user": "u001"}, ip="10.1.33.9"), None)
        check("gateway：白名单来源 + 可信头 → 解析出身份且 trusted=True",
              r["ok"] and r["user"]["user_id"] == "u001"
              and r["user"]["identity_trusted"] is True
              and r["user"]["identity_header"] == "x-auth-user")
        r = resolve(_FakeRequest(query={"user_id": "u001"}, ip="10.1.33.9"), None)
        check("gateway：**参数里的 user_id 一律忽略**（否则网关鉴权被查询参数绕过）",
              (not r["ok"]) and r["reason"] == "no_identity_header")
        evs = meta_db.list_auth_events(kind="header_rejected")
        check("伪造头被拒时留痕（谁从哪试过，docs/12 §4.4）", len(evs) >= 2, f"{len(evs)} 条")

        # ④ sso：CAS 票据校验（mock 掉网络）
        settings.auth_mode = "sso"
        settings.cas_base_url = "https://sso.hospital.local/cas"
        settings.sso_service_url = "https://ask.hospital.local"
        calls = {}

        def fake_get(url):
            calls["url"] = url
            if "ticket=GOOD" in url:
                return 200, json.dumps({"serviceResponse": {"authenticationSuccess": {
                    "user": "u002", "attributes": {"cn": ["心内科王主任"],
                                                   "deptCode": ["0101"], "campusCode": ["01"]}}}})
            if "ticket=XML" in url:
                # 11g 时代的老 CAS：即使要 JSON 也只给 XML（真实踩坑点）
                return 200, ("<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>"
                             "<cas:authenticationSuccess><cas:user>u003</cas:user>"
                             "<cas:attributes><cas:cn>李干事</cas:cn>"
                             "<cas:deptCode>0102</cas:deptCode></cas:attributes>"
                             "</cas:authenticationSuccess></cas:serviceResponse>")
            return 200, ("<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>"
                         "<cas:authenticationFailure code='INVALID_TICKET'>票据无效"
                         "</cas:authenticationFailure></cas:serviceResponse>")
        global http_get, http_post_form
        http_get = fake_get
        r = cas_validate("GOOD-ST-TICKET", "https://ask.hospital.local/api/security/callback")
        check("CAS：票据有效 → 得到工号与属性",
              r["ok"] and r["user_id"] == "u002" and r["attrs"]["deptCode"] == "0101",
              str(r.get("error", "")))
        check("CAS：service 参数与配置一致（不一致必校验失败，这是最常见的踩坑点）",
              "service=https%3A%2F%2Fask.hospital.local%2Fapi%2Fsecurity%2Fcallback" in calls["url"])
        r = cas_validate("XML", "https://ask.hospital.local/api/security/callback")
        check("CAS：老服务端只回 XML 时也能解析（不能只认一种响应格式）",
              r["ok"] and r["user_id"] == "u003" and r["attrs"].get("deptCode") == "0102",
              str(r.get("error", ""))[:60])
        r = cas_validate("BAD", "https://ask.hospital.local/api/security/callback")
        check("CAS：票据无效 → 明确失败（不降级为匿名）", not r["ok"], str(r.get("error"))[:60])

        # ⑤ 回调：建会话 + 属性映射
        cb = handle_callback(_FakeRequest(query={"ticket": "GOOD"}))
        check("回调：CAS 成功 → 返回工号与姓名/科室/院区",
              cb["ok"] and cb["user_id"] == "u002" and cb["name"] == "心内科王主任"
              and cb["dept_code"] == "0101", str(cb.get("error", "")))
        check("回调：未带 ticket → 明确报错", not handle_callback(_FakeRequest())["ok"])

        settings.attr_user = "employee_no"
        def fake_get2(url):
            return 200, json.dumps({"serviceResponse": {"authenticationSuccess": {
                "user": "u002", "attributes": {"employee_no": ["u004"]}}}})
        http_get = fake_get2
        cb = handle_callback(_FakeRequest(query={"ticket": "GOOD"}))
        check("属性名可配（院方平台字段名不同，改配置不改代码）",
              cb["ok"] and cb["user_id"] == "u004", str(cb.get("error", ""))[:60])
        # 取不到工号时必须失败，而不是拿 name 兜底
        http_get = lambda url: (200, json.dumps({"serviceResponse": {"authenticationSuccess": {
            "user": "", "attributes": {"cn": ["王主任"]}}}}))
        cb = handle_callback(_FakeRequest(query={"ticket": "GOOD"}))
        check("取不到工号 → 登录失败（不许用姓名兜底，姓名不唯一）",
              (not cb["ok"]) and "工号" in cb["error"])
        http_get = fake_get

        # ⑥ OIDC：state 防重放 + code 换主体
        settings.sso_protocol = "oidc"
        settings.oidc_issuer = "https://sso.hospital.local/oidc"
        settings.oidc_client_id = "ask-data"
        settings.attr_user = "uid"
        st = make_state()
        check("OIDC state 校验通过", check_state(st))
        check("OIDC state 被篡改 → 拒绝", not check_state(st[:-1] + ("0" if st[-1] != "0" else "1")))
        check("OIDC state 过期 → 拒绝",
              not check_state(f"{int(time.time()) - STATE_TTL - 5}.{st.split('.')[1]}"))
        check("OIDC state 缺席 → 拒绝", not check_state(None))
        lu = login_url(_FakeRequest())
        check("OIDC 登录地址带 state 且指向 /authorize",
              lu["ok"] and "state=" in lu["url"] and "/authorize" in lu["url"])

        def fake_post(url, data):
            check("OIDC：用授权码换令牌（POST /token）",
                  url.endswith("/token") and data.get("code") == "CODE1")
            return 200, json.dumps({"access_token": "AT1"})
        def fake_get3(url):
            return 200, json.dumps({"uid": "u002", "cn": "心内科王主任"})
        http_post_form = fake_post
        http_get = fake_get3
        r = handle_callback(_FakeRequest(query={"code": "CODE1", "state": make_state()}))
        check("OIDC 回调 → 工号", r["ok"] and r["user_id"] == "u002", str(r.get("error", ""))[:60])
        r = handle_callback(_FakeRequest(query={"code": "CODE1", "state": "forged.1"}))
        check("OIDC 回调 state 伪造 → 拒绝（不给任何人换主体的机会）", not r["ok"])

        # ⑦ 配置缺失时如实说缺什么（不是抛一个看不懂的异常）
        settings.oidc_issuer = ""
        lu = login_url(_FakeRequest())
        check("SSO 参数未配置 → 明确列出缺哪一项（docs/12 §3.5 先要东西再动手）",
              (not lu["ok"]) and lu["missing"] == ["ASK_OIDC_ISSUER"], str(lu["missing"]))
        settings.oidc_issuer = "https://sso.hospital.local/oidc"

        # ⑧ sso 模式的请求解析：认服务端会话，不认参数
        s = session.create("u002", "科室主任", auth_mode="sso", auth_source="cas")
        r = resolve(_FakeRequest(query={"user_id": "u001"}), None)
        check("sso：无会话 → login_required（**不回落成 guest 出数**）",
              (not r["ok"]) and r["reason"] == "login_required")
        r = resolve(_FakeRequest(cookies={settings.session_cookie_name: s["token"]},
                                 query={"user_id": "u001"}), None)
        check("sso：有会话 → 身份来自会话，参数里的 user_id 被忽略",
              r["ok"] and r["user"]["user_id"] == "u002"
              and r["user"]["identity_source"] == "sso")
        check("sso：授权**每次重新解析**（会话只证明工号，不缓存权限）",
              r["user"]["dept_scope"] == ["0101"], str(r["user"]["dept_scope"]))
        session.revoke(s["token"], "logout")
        r = resolve(_FakeRequest(cookies={settings.session_cookie_name: s["token"]}), None)
        check("sso：登出后同一 Cookie 立即失效 → login_required",
              (not r["ok"]) and r["reason"] == "login_required")
        check("会话失效也留痕（auth_event: session_expired）",
              bool(meta_db.list_auth_events(kind="session_expired")))

        # ⑨ 身份说明必须照实（界面上不能把演示模式说成已鉴权）
        settings.auth_mode = "demo"
        check("身份说明：demo 直说「不是安全边界」",
              "不是安全边界" in identity_note({"identity_source": "demo"}))
        check("身份说明：sso 说清「票据校验后的服务端会话」",
              "服务端会话" in identity_note({"identity_source": "sso"}))
    finally:
        http_get = None
        http_post_form = None
        settings.meta_db_path = old[0]
        settings.auth_mode = old[1]
        settings.trusted_proxies = old[2]
        settings.cas_base_url = old[3]
        settings.sso_service_url = old[4]
        settings.oidc_issuer = old[5]
        settings.oidc_client_id = old[6]
        settings.attr_user = old[7]
        settings.sso_protocol = old[8]
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
