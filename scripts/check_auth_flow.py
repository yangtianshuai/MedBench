# -*- coding: utf-8 -*-
"""身份授信验收（docs/12 §7 A 组 + D3）—— **起真服务、走真 HTTP**。

跑法（在仓库根目录）：
    python scripts/check_auth_flow.py

为什么不能只用 `app/core/identity.py --selftest` 交差：
    模块自测证明的是"判定逻辑对"，证明不了"接线对"—— 票据校验通过之后
    有没有真的建会话、Cookie 有没有真的写到响应上、登出之后是不是真失效、
    拒绝有没有留痕，这些都只有在真 HTTP 上才看得见。
    本脚本自带一个 **mock 统一身份平台**（CAS + OIDC），所以不依赖院方环境。

覆盖（H 系列）：
    H1  A1  未登录访问 → need_login / user 为空（**不回落**成任何默认用户）
    H2      登录跳转的 service 参数正确（这个参数写错，票据校验必失败）
    H3      回调 → 建服务端会话 → Set-Cookie（HttpOnly / SameSite=Lax）
    H4      带会话访问 → 身份与 roles.yaml 一致
    H5  A3  伪造身份头无效
    H6      登出 → 服务端会话**立即**失效
    H7  D3  身份事件留痕（login / logout / login_failed）
    H8  E3  未登录不能读在线会话清单
    H9  A4  demo 模式绑 0.0.0.0 → 拒绝启动（真起进程看退出码）
    H10     票据无效 → 跳回首页带 auth_error + 留痕（不静默失败）
    H11  ~  OIDC：登录地址带 state；state 伪造/缺失一律拒绝
    H12     老 CAS 只回 XML 时照样能登录（院方 11g 时代的老平台）

退出码：0 全过；1 有失败。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SSO_PORT = 8128
SSO_BASE = f"http://127.0.0.1:{SSO_PORT}"

passed = True
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, total
    total += 1
    if not cond:
        passed = False
    print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""), flush=True)


# ── mock 统一身份平台（CAS + OIDC，同一端口不同路径）──
class _SsoHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):              # 静音，别把日志混进验收输出
        pass

    def _send(self, code: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                                    # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path.endswith("/serviceValidate"):
            ticket = (q.get("ticket") or [""])[0]
            if ticket == "ST-GOOD":
                self._send(200, json.dumps({"serviceResponse": {"authenticationSuccess": {
                    "user": "u002",
                    "attributes": {"cn": ["心内科王主任"], "deptCode": ["0101"],
                                   "campusCode": ["01"]}}}}))
            elif ticket == "ST-XML":
                # 11g 时代的老 CAS：要 JSON 也只回 XML（真实踩坑点）
                self._send(200, "<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>"
                                "<cas:authenticationSuccess><cas:user>u002</cas:user>"
                                "<cas:attributes><cas:cn>心内科王主任</cas:cn>"
                                "<cas:deptCode>0101</cas:deptCode></cas:attributes>"
                                "</cas:authenticationSuccess></cas:serviceResponse>")
            else:
                self._send(200, json.dumps({"serviceResponse": {"authenticationFailure": {
                    "code": "INVALID_TICKET", "description": "票据无效"}}}))
            return
        if u.path.endswith("/userinfo"):
            self._send(200, json.dumps({"uid": "u002", "cn": "心内科王主任"}))
            return
        self._send(404, "{}")

    def do_POST(self):                                   # noqa: N802
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode())
        if u.path.endswith("/token"):
            if (form.get("code") or [""])[0] == "CODE-GOOD":
                self._send(200, json.dumps({"access_token": "AT-1"}))
            else:
                self._send(400, json.dumps({"error": "invalid_grant"}))
            return
        self._send(404, "{}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不跟随跳转：本脚本要看的就是 302 到哪里、带什么 Cookie。"""

    def redirect_request(self, *a, **k):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def request(port: int, path: str, method: str = "GET", body: dict = None,
            cookie: str = None, headers: dict = None):
    """返回 (status, body, headers)。★ 头名统一转小写：ASGI 服务端发的是小写，"""
    data = json.dumps(body).encode() if body is not None else None
    h = dict(headers or {})
    if data:
        h["Content-Type"] = "application/json"
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data,
                                 method=method, headers=h)
    try:
        with _opener.open(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8", "replace"), _lower(dict(r.headers))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), _lower(dict(e.headers))


def _lower(h: dict) -> dict:
    return {str(k).lower(): v for k, v in h.items()}


def wait_ready(port: int, timeout: float = 30.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s, _, _ = request(port, "/api/health")
            if s == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def _env(**extra) -> dict:
    e = dict(os.environ)
    e.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
              "ASK_META_DB": str(ROOT / "var" / "_authflow_check.db")})
    e.update({k: str(v) for k, v in extra.items()})
    return e


@contextmanager
def app(port: int, **env_extra):
    """起一个真服务，退出时收掉。"""
    p = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=_env(**env_extra),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace")
    if not wait_ready(port):
        out = ""
        try:
            out = (p.stdout.read() if p.stdout else "")[-1500:]
        except Exception:
            pass
        p.terminate()
        raise RuntimeError(f"服务未就绪（端口 {port}）：\n{out}")
    try:
        yield p
    finally:
        p.terminate()
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


def _events():
    """直接读验收用的元数据库（脚本自带隔离库，不碰真实库）。"""
    sys.path.insert(0, str(ROOT))
    from app.core import meta_db
    meta_db.settings.meta_db_path = str(ROOT / "var" / "_authflow_check.db")
    return meta_db


# ════════════════════════════════════════════════════════════
def cas_phase() -> None:
    port = 8127
    base = f"http://127.0.0.1:{port}"
    with app(port, ASK_AUTH_MODE="sso", ASK_SSO_PROTOCOL="cas",
             ASK_CAS_BASE=SSO_BASE, ASK_SSO_SERVICE_URL=base, ASK_ATTR_USER="uid"):
        # H1：A1 —— 未登录必须"没有身份"，不得回落到任何默认用户
        s, b, _ = request(port, "/api/security/session")
        j = json.loads(b)
        check("H1 未登录：need_login=true 且 user 为空（A1 不回落默认用户）",
              s == 200 and j.get("user") is None and j.get("need_login") is True, f"status={s}")
        check("H1b 未登录：不返回演示账号（前端不该出现身份切换器）",
              j.get("demo_accounts") == [], str(j.get("demo_accounts")))
        s, _, h = request(port, "/api/security/sessions")
        check("H8 未登录读在线会话清单 → 401（不是 GET 就放开）", s == 401, f"status={s}")

        # H2：登录跳转的 service 必须与登记的一致
        s, _, h = request(port, "/api/security/login")
        loc = h.get("location") or ""
        want = urllib.parse.quote(f"{base}/api/security/callback", safe="")
        check("H2 /login → 302 到统一身份平台，且 service 参数与登记一致",
              s == 302 and loc.startswith(SSO_BASE) and want in loc, loc[:100])

        # H10：票据无效 → 回首页带原因，且**不建会话**
        s, _, h = request(port, "/api/security/callback?ticket=ST-BAD")
        loc = h.get("location") or ""
        check("H10 票据无效 → 302 回首页带 auth_error，且不写 Cookie",
              s == 302 and "auth_error=" in loc and "set-cookie" not in h, loc[:90])

        # H12：老 CAS 只回 XML 时照样能登录
        s, _, h = request(port, "/api/security/callback?ticket=ST-XML")
        xml_cookie = (h.get("set-cookie") or "").split(";")[0]
        check("H12 老 CAS 只回 XML → 也能登录（不能只认一种响应格式）",
              s == 302 and xml_cookie.startswith("ask_session="), (h.get("location") or "")[:60])

        # H3：票据有效 → 建会话 + 写 Cookie
        s, _, h = request(port, "/api/security/callback?ticket=ST-GOOD")
        sc = h.get("set-cookie") or ""
        check("H3 票据有效 → 302 回首页并下发会话 Cookie",
              s == 302 and "ask_session=" in sc, sc[:80])
        check("H3b Cookie 带 HttpOnly + SameSite=lax（脚本读不到 / 跨站提交挡掉）",
              "httponly" in sc.lower() and "samesite=lax" in sc.lower(), sc[:120])
        cookie = sc.split(";")[0]

        # H4：带会话 → 身份与 roles.yaml 一致
        s, b, _ = request(port, "/api/security/session", cookie=cookie)
        u = (json.loads(b).get("user") or {})
        check("H4 带会话 → 身份取自 SSO 票据（u002 / 科室主任 / 科室 0101 / trusted）",
              s == 200 and u.get("user_id") == "u002" and u.get("role") == "科室主任"
              and u.get("dept_scope") == ["0101"] and u.get("identity_trusted") is True,
              f"{u.get('user_id')}/{u.get('role')}/{u.get('dept_scope')}")
        check("H4b 身份来源标为 sso（授权每次重新解析，会话只证明工号）",
              u.get("identity_source") == "sso")

        # H5：A3 —— 伪造身份头无效
        s, b, _ = request(port, "/api/security/session", cookie=None,
                          headers={"X-Auth-User": "u001", "X-Forwarded-User": "u001"})
        u = (json.loads(b).get("user") or {})
        check("H5 伪造 X-Auth-User 头 → 拿不到院长身份（sso 不认头）",
              u.get("user_id") != "u001", str(u)[:60])

        # H6：登出 → 服务端立即失效
        s, b, _ = request(port, "/api/security/logout", method="POST", cookie=cookie)
        j = json.loads(b)
        check("H6 登出 → ok 且真的撤销了一条会话",
              s == 200 and j.get("ok") is True and j.get("revoked") is True, b[:80])
        s, b, _ = request(port, "/api/security/session", cookie=cookie)
        j = json.loads(b)
        check("H6b 登出后同一 Cookie 立即失效（只清 Cookie 不算登出）",
              j.get("user") is None and j.get("need_login") is True)
        # 老 CAS 那条会话也应有效（证明 XML 分支真的建了会话）
        s, b, _ = request(port, "/api/security/session", cookie=xml_cookie)
        check("H12b XML 分支建的会话可用", s == 200 and
              (json.loads(b).get("user") or {}).get("user_id") == "u002")

    # H7：D3 —— 身份事件留痕（会话与事件都在验收库里）
    db = _events()
    kinds = {e["kind"] for e in db.list_auth_events(limit=80)}
    check("H7 身份事件留痕：login / logout / login_failed 都在",
          {"login", "logout", "login_failed"} <= kinds, str(sorted(kinds)))
    fails = [e for e in db.list_auth_events(kind="login_failed", limit=20)
             if "票据" in str(e.get("detail"))]
    check("H7b 票据失败的原因被记下来（排查不必翻服务日志）",
          bool(fails), str(fails[0].get("detail"))[:60] if fails else "无票据类失败事件")
    sess = db.list_user_sessions(include_revoked=True)
    check("H7c 会话能区分「主动登出」与「被踢」",
          any(r.get("revoked_reason") for r in sess),
          str(sorted({str(r.get("revoked_reason")) for r in sess})))
    check("H7d 在线会话清单**不返回 token**（凭据不该出现在列表里）",
          all("token" not in r for r in sess), f"{len(sess)} 条")


def oidc_phase() -> None:
    port = 8129
    base = f"http://127.0.0.1:{port}"
    with app(port, ASK_AUTH_MODE="sso", ASK_SSO_PROTOCOL="oidc",
             ASK_OIDC_ISSUER=SSO_BASE, ASK_OIDC_CLIENT_ID="ask-data",
             ASK_SSO_SERVICE_URL=base, ASK_ATTR_USER="uid"):
        s, _, h = request(port, "/api/security/login")
        loc = h.get("location") or ""
        check("H11 OIDC /login → 302 到 /authorize 且带 state（防跨站伪造回调）",
              s == 302 and "/authorize" in loc and "state=" in loc, loc[:100])
        state = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("state", [""])[0]

        s, _, h = request(port, "/api/security/callback?code=CODE-GOOD&state=forged.1")
        check("H11b state 伪造 → 拒绝，不建会话",
              s == 302 and "auth_error=" in (h.get("location") or "")
              and "set-cookie" not in h)
        s, _, h = request(port, "/api/security/callback?code=CODE-GOOD")
        check("H11c state 缺失 → 拒绝（OIDC 回调不能裸奔）",
              s == 302 and "auth_error=" in (h.get("location") or "")
              and "set-cookie" not in h)
        s, _, h = request(port, f"/api/security/callback?code=CODE-GOOD&state={state}")
        sc = h.get("set-cookie") or ""
        check("H11d state 正确 + 授权码有效 → 建会话（OIDC 全链路通）",
              s == 302 and "ask_session=" in sc, sc[:80])
        if sc:
            s, b, _ = request(port, "/api/security/session", cookie=sc.split(";")[0])
            u = (json.loads(b).get("user") or {})
            check("H11e OIDC 身份解析到工号（ASK_ATTR_USER 映射生效）",
                  u.get("user_id") == "u002" and u.get("role") == "科室主任",
                  f"{u.get('user_id')}/{u.get('role')}")


def _cookie_of(headers: dict) -> str:
    m = re.search(r"ask_session=([^;]+)", headers.get("set-cookie", "") or "")
    return f"ask_session={m.group(1)}" if m else ""


def local_phase() -> None:
    """H30~H40：本地账号兜底（docs/12 §3.6）—— 起真服务、走真 HTTP。

    与 SSO 用例刻意分开：这条链路的"凭证"是口令而不是票据，
    但两者**必须落在同一种服务端会话上** —— 否则登出/超时/踢人会出现两套行为。
    """
    port = 8131
    db = ROOT / "var" / "_authlocal_check.db"
    _cleanup_db(db)
    try:
        # ① CLI 自举：账号表与台账都为空时，没有任何人能登录 —— 只能从命令行破局
        env = _env(ASK_META_DB=str(db), ASK_AUTH_LOCAL="primary", ASK_AUTH_MODE="sso")
        p = subprocess.run(
            [sys.executable, "-m", "app.core.local_accounts", "--create-admin", "emg001",
             "--name", "应急管理员", "--reason", "验收：信息科备案 2026-10-17"],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
        m = re.search(r"一次性初始口令：(\S+)", out)
        check("H30 CLI 自举建管理员（账号与台账都为空时唯一的入口）",
              p.returncode == 0 and bool(m),
              (out.strip().splitlines()[-1] if out.strip() else ""))
        first_pwd = m.group(1) if m else ""

        with app(port, ASK_META_DB=str(db), ASK_AUTH_MODE="sso", ASK_AUTH_LOCAL="primary"):
            # ② 状态接口公开可读（登录页据此渲染，不靠猜）
            s, b, _ = request(port, "/api/security/auth/local/status")
            j = json.loads(b)
            # ★ 最小长度 8 是**硬下限**（配置只能往上调），前端提示读的就是这个值
            check("H30b 登录入口状态公开可读（含档位与最小口令长度 8）",
                  s == 200 and j.get("enabled") is True and j.get("mode") == "primary"
                  and int(j.get("pwd_min_len") or 0) == 8, f"status={s}")

            # ③ 错误口令：与"账号不存在"返回**同一句话**，否则登录页就是工号枚举器
            s1, b1, _ = request(port, "/api/security/auth/local/login", "POST",
                                {"user_id": "emg001", "password": "wrong-password"})
            s2, b2, _ = request(port, "/api/security/auth/local/login", "POST",
                                {"user_id": "no-such-user", "password": "wrong-password"})
            check("H31 错误口令与「账号不存在」同答（防工号枚举）",
                  s1 == 401 and s2 == 401
                  and json.loads(b1).get("detail") == json.loads(b2).get("detail"),
                  f"{s1}/{s2}")

            # ④ 正确口令 → 与 SSO 同一种服务端会话 Cookie
            s, b, h = request(port, "/api/security/auth/local/login", "POST",
                              {"user_id": "emg001", "password": first_pwd})
            ck = h.get("set-cookie", "")
            cookie = _cookie_of(h)
            j = json.loads(b) if b else {}
            check("H32 登录成功：Cookie 只放不透明令牌（不含工号/角色，HttpOnly）",
                  s == 200 and bool(cookie) and "emg001" not in ck
                  and "httponly" in ck.lower(), f"status={s}")
            check("H32b 首次登录要求改密（管理员不该知道用户最终口令）",
                  j.get("must_change") is True)

            # ⑤ 未改密前：其它接口由**服务端中间件**拦成 409（不靠界面自觉）
            s, b, _ = request(port, "/api/security/audit", cookie=cookie)
            check("H33 未改密 → 其它接口 409（拿着初始口令不能出数）",
                  s == 409 and json.loads(b).get("must_change") is True, f"status={s}")

            # ⑥ 改密 → 放行；admin 权限来自授权台账，不是来自账号表
            new_pwd = "Oncology@2026xyz"
            s, b, _ = request(port, "/api/security/auth/local/password/change", "POST",
                              {"old_password": first_pwd, "new_password": new_pwd},
                              cookie=cookie)
            check("H34 本人改密成功", s == 200, f"status={s}")
            s, b, _ = request(port, "/api/security/audit", cookie=cookie)
            check("H34b 改密后管理端接口可用（admin 来自台账，与账号表无关）",
                  s == 200, f"status={s}")

            # ⑦ 锁定：连续失败达上限 → 锁定期内**正确口令也拒绝**
            for _ in range(5):
                request(port, "/api/security/auth/local/login", "POST",
                        {"user_id": "emg001", "password": "wrong-password"})
            s, b, _ = request(port, "/api/security/auth/local/login", "POST",
                              {"user_id": "emg001", "password": new_pwd})
            detail = str(json.loads(b).get("detail", ""))
            check("H35 锁定后正确口令也拒绝（否则「锁 15 分钟」对爆破毫无意义）",
                  s == 401 and "锁定" in detail, detail[:48])

            # ⑧ 管理员解锁 + 建号（默认停用）+ 启用
            s, _, _ = request(port, "/api/security/local/users/unlock", "POST",
                              {"user_id": "emg001"}, cookie=cookie)
            check("H36 管理员解锁", s == 200, f"status={s}")

            s, b, _ = request(port, "/api/security/local/users", "POST",
                              {"user_id": "doc001", "name": "验收医生",
                               "reason": "验收：临时账号", "valid_days": 30},
                              cookie=cookie)
            j = json.loads(b) if b else {}
            doc_pwd = j.get("initial_password") or ""
            check("H37 建号默认停用（建好就能用，等于没人复核过）",
                  s == 200 and j.get("status") == "disabled" and bool(doc_pwd),
                  f"status={s}")

            s, b, _ = request(port, "/api/security/auth/local/login", "POST",
                              {"user_id": "doc001", "password": doc_pwd})
            check("H37b 停用账号拒绝登录（原因可读，不是「口令错」）",
                  s == 401 and "停用" in str(json.loads(b).get("detail", "")),
                  f"status={s}")

            s, _, _ = request(port, "/api/security/local/users/status", "POST",
                              {"user_id": "doc001", "status": "active", "reason": "验收"},
                              cookie=cookie)
            s2, b2, _ = request(port, "/api/security/auth/local/login", "POST",
                                {"user_id": "doc001", "password": doc_pwd})
            check("H38 启用后可登录（启用 ≠ 授权；权限仍按台账）",
                  s == 200 and s2 == 200, f"{s}/{s2}")

            # ⑨ 重置口令 → 该人全部会话立刻失效
            s, b, _ = request(port, "/api/security/local/users/password", "POST",
                              {"user_id": "emg001", "reason": "验收：重置"},
                              cookie=cookie)
            j = json.loads(b) if b else {}
            check("H39 重置口令会撤销全部会话（只改口令不踢人＝旧 Cookie 还能用）",
                  s == 200 and int(j.get("revoked_sessions") or 0) >= 1, f"status={s}")
            s, b, _ = request(port, "/api/security/session", cookie=cookie)
            check("H39b 旧会话已失效（/session 回到 need_login）",
                  json.loads(b).get("need_login") is True)

        # ⑩ off 档位：入口必须消失，且**不静默降级**
        with app(port + 1, ASK_META_DB=str(db), ASK_AUTH_MODE="sso", ASK_AUTH_LOCAL="off"):
            s, b, _ = request(port + 1, "/api/security/auth/local/status")
            check("H40 off 档位：状态 enabled=false",
                  s == 200 and json.loads(b).get("enabled") is False)
            s, _, _ = request(port + 1, "/api/security/auth/local/login", "POST",
                              {"user_id": "emg001", "password": "whatever"})
            check("H40b off 档位：登录接口 404（不静默降级成别的模式）", s == 404, f"status={s}")
    finally:
        _cleanup_db(db)


def bootstrap_phase() -> None:
    """H50~H58：空系统首次初始化 + 测试数据一键清理 —— 起真服务、走真 HTTP。

    为什么这条链路必须真起服务：初始化入口是**公开写接口**，
    它是否只在"账号表为空"时可用、建成后是否真的关闭、清理后审计链是否自洽，
    都只能在真 HTTP + 真库上验证。
    """
    port = 8133
    db = ROOT / "var" / "_authboot_check.db"
    audit_db = ROOT / "var" / "_authboot_check_audit.db"
    key = ROOT / "var" / "_authboot_check.key"
    anchor = ROOT / "var" / "_authboot_check_anchor.jsonl"
    for f in (db, audit_db, key, anchor):
        _cleanup_db(f)
    env = dict(ASK_META_DB=str(db), ASK_AUDIT_DB=str(audit_db),
               ASK_AUDIT_KEY_FILE=str(key), ASK_AUDIT_ANCHOR=str(anchor),
               ASK_AUTH_MODE="sso", ASK_AUTH_LOCAL="primary", ASK_AUTHZ_FROM="auto")
    try:
        with app(port, **env):
            # ① 空系统：初始化入口公开可读，客户端据此渲染建号表单
            s, b, _ = request(port, "/api/security/bootstrap/status")
            j = json.loads(b) if b else {}
            check("H50 空系统：初始化入口公开可读（needs_bootstrap=true）",
                  s == 200 and j.get("needs_bootstrap") is True, f"status={s}")
            s, b, _ = request(port, "/api/security/session")
            j = json.loads(b) if b else {}
            check("H50b 会话接口带出初始化状态（渲染建号表单而不是登录框）",
                  (j.get("bootstrap") or {}).get("needs_bootstrap") is True)

            # ② 口令策略不是后门：初始化也走同一条策略（8 位下限）
            s, _, _ = request(port, "/api/security/bootstrap/admin", "POST",
                              {"user_id": "adm01", "name": "管理员",
                               "password": "Abc1234", "password2": "Abc1234"})
            check("H51 初始化口令 7 位 → 拒绝（下限是 8 位，不走后门）", s == 400,
                  f"status={s}")

            # ③ 建成即登录，且管理权来自台账；口令**恰好 8 位**即可用
            s, b, h = request(port, "/api/security/bootstrap/admin", "POST",
                              {"user_id": "adm01", "name": "管理员",
                               "password": "Abcd1234",
                               "password2": "Abcd1234"})
            j = json.loads(b) if b else {}
            cookie = _cookie_of(h)
            check("H52 创建第一个管理员并直接登录（Cookie 只放不透明令牌）",
                  s == 200 and j.get("is_admin") is True and bool(cookie)
                  and "adm01" not in (h.get("set-cookie") or ""), f"status={s}")

            s, b, _ = request(port, "/api/security/bootstrap/status")
            check("H53 建成后初始化入口立刻关闭",
                  json.loads(b).get("needs_bootstrap") is False)
            s, _, _ = request(port, "/api/security/bootstrap/admin", "POST",
                              {"user_id": "adm02", "name": "第二人",
                               "password": "Hospital@2026abc",
                               "password2": "Hospital@2026abc"})
            check("H53b 入口关闭后再调 → 400（不能借它造第二个管理员）",
                  s == 400, f"status={s}")

            s, _, _ = request(port, "/api/security/local/users", cookie=cookie)
            check("H54 初始化账号真有管理权（admin 来自授权台账，不是账号表）",
                  s == 200, f"status={s}")

            # ④ 测试数据一键清理
            s, _, _ = request(port, "/api/chat/sessions", "POST", {"title": "验收会话"},
                              cookie=cookie)
            check("H55 造一条测试数据（会话）", s == 200, f"status={s}")
            s, b, _ = request(port, "/api/admin/maintenance/inventory", cookie=cookie)
            j = json.loads(b) if b else {}
            chat = next((g for g in (j.get("groups") or []) if g["code"] == "chat"), {})
            check("H55b 清理现状可读（会话组 1 条）",
                  s == 200 and (chat.get("counts") or {}).get("chat_session") == 1,
                  f"status={s}")

            s, _, _ = request(port, "/api/admin/maintenance/test-data/clear", "POST",
                              {"scopes": ["chat"], "confirm": "随便"}, cookie=cookie)
            check("H56 清理必须手打确认短语（勾选框会被顺手点上）", s == 400, f"status={s}")

            s, b, _ = request(port, "/api/admin/maintenance/test-data/clear", "POST",
                              {"scopes": ["chat", "qa", "identity"],
                               "confirm": "我确认清理测试数据"}, cookie=cookie)
            j = json.loads(b) if b else {}
            check("H57 一键清理执行成功", s == 200 and j.get("ok") is True, f"status={s}")
            s, b, _ = request(port, "/api/admin/maintenance/inventory", cookie=cookie)
            j = json.loads(b) if b else {}
            by = {g.get("code"): (g.get("total") or 0) for g in (j.get("groups") or [])}
            check("H57b 清完只剩账号组与「清理留痕」1 条（审计/会话归零）",
                  by.get("qa") == 0 and by.get("chat") == 0 and by.get("identity", 0) <= 1,
                  str(by))
            s, b, _ = request(port, "/api/security/events?kind=test_data_cleared",
                              cookie=cookie)
            check("H57c 清理动作本身留痕（否则「数据什么时候没的」说不清）",
                  s == 200 and len(json.loads(b) or []) >= 1, f"status={s}")

            s, b, _ = request(port, "/api/security/audit/verify", cookie=cookie)
            j = json.loads(b) if b else {}
            check("H58 审计复位后链与锚自洽（不误报「尾部被删」）",
                  s == 200 and j.get("ok") is True,
                  f"status={s} reason={str(j.get('reason'))[:40]}")
    finally:
        for f in (db, audit_db, key, anchor):
            _cleanup_db(f)


def a4_phase() -> None:
    """H9（A4）：demo 模式绑 0.0.0.0 必须**起不来** —— 这条只能真起进程看退出码。"""
    p = subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0",
         "--port", "8131", "--log-level", "info"],
        cwd=str(ROOT), env=_env(ASK_AUTH_MODE="demo"),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
    out = (p.stdout or "") + (p.stderr or "")
    check("H9 A4：demo 绑 0.0.0.0 → 拒绝启动（非零退出码 + 说明原因）",
          p.returncode != 0 and "拒绝启动" in out,
          f"rc={p.returncode} 提示可见={'拒绝启动' in out}")


def ledger_phase() -> None:
    """H13~H20：授权台账（docs/12 §4.2）—— **台账改了，SQL 里的围栏真的跟着变**。

    这一段只在真服务上才做得出来：模块自测能证明"解析出的范围对"，
    证明不了"这个范围真的进了 SQL 的 WHERE"。而后者才是"下钻不绕过"的含义。
    """
    port = 8133
    # ★ 文件名带 pid：验收用的库绝不能"复用上一轮的授权"（那会让用例时好时坏）。
    #   `meta_db._conn()` 返回的是裸连接，`with conn:` 只提交事务、不关闭句柄
    #   （靠 CPython 引用计数兜底），所以 Windows 上文件可能要到进程退出才真正释放 ——
    #   删不掉就删不掉，换个名字即可，正确性不依赖"删得掉"。
    db = ROOT / "var" / f"_authledger_check_{os.getpid()}.db"
    # 审计也要给出**独立的**临时库：这一段的 H21 要断言"审计真的不在业务库里"，
    # 用默认的 var/audit.db 就验证不了这件事（也可能污染真实审计）。
    audit_db = ROOT / "var" / f"_authledger_audit_{os.getpid()}.db"
    audit_key = ROOT / "var" / f"_authledger_{os.getpid()}.key"
    audit_anchor = ROOT / "var" / f"_authledger_{os.getpid()}.anchor.jsonl"
    for f in (db, audit_db, audit_key, audit_anchor):
        _cleanup_db(f)

    sys.path.insert(0, str(ROOT))
    from app.config import settings as _s
    from app.core import authz_sync
    # ★ 本进程（CLI 侧）与服务进程必须用**同一把审计密钥、同一个审计库** ——
    #   否则 CLI 写的留痕行会看起来像"被改过"（键不同 → 哈希不同），
    #   这是真实部署里也该遵守的约定（密钥由信息科统一分发，不是每台机器自己生成）。
    old_db, old_from = _s.meta_db_path, _s.authz_from
    old_audit = (_s.audit_path, _s.audit_key_path, _s.audit_anchor_path)
    _s.audit_path = str(audit_db)
    _s.audit_key_path = str(audit_key)
    _s.audit_anchor_path = str(audit_anchor)

    with app(port, ASK_DS="ads", ASK_LLM_MOCK="1", ASK_AUTH_MODE="demo",
             ASK_AUTHZ_FROM="table", ASK_META_DB=str(db),
             ASK_AUDIT_DB=str(audit_db), ASK_AUDIT_KEY_FILE=str(audit_key),
             ASK_AUDIT_ANCHOR=str(audit_anchor), ASK_AUDIT_ANCHOR_EVERY="2"):
        _s.meta_db_path, _s.authz_from = str(db), "table"
        try:
            # H13：台账为空的严格模式 —— 谁都没权限，**连管理端也进不去**
            #      （这是"鸡生蛋"：首启必须先用命令行迁移一次，见下面的 bootstrap）
            s, _, _ = request(port, "/api/security/grants?user_id=u005")
            check("H13 台账为空的严格模式：管理员连自己的授权页都进不去（必须先 CLI 迁移）",
                  s == 403, f"status={s}")
            s, b, _ = request(port, "/api/ask", method="POST",
                              body={"question": "2023年出院人次是多少", "user_id": "u002"})
            j = json.loads(b)
            check("H13b 台账里没有授权 → 问数被拒（空范围=无授权，不回落 YAML）",
                  j.get("status") == "denied" and not j.get("rows"),
                  f"status={j.get('status')}")

            # bootstrap：命令行迁移（等价于 python scripts/sync_authz.py --source yaml --apply）
            r = authz_sync.run(source="yaml", ref="MIGRATE-check", apply=True)
            check("H13c 迁移：roles.yaml 名单进台账（5 人），依据写入留痕",
                  r["ok"] and r["result"]["grant"] == 5, str(r.get("counts")))

            # H14：台账授权后，**科室级授权的 SQL 真的能跑出数**
            #      （这一段是本轮最重要的回归：`belong_dept_code` 曾被写死成一个
            #        两条路线都不存在的列，导致科室级用户一条数都查不出来）
            def total_of(uid, question="2023年出院人次是多少"):
                s, b, _ = request(port, "/api/ask", method="POST",
                                  body={"question": question, "user_id": uid})
                j = json.loads(b)
                rows = j.get("rows") or []
                val = None
                if rows and isinstance(rows[0], dict):
                    for k, v in rows[0].items():
                        if k not in ("stat_period", "period", "grp") and isinstance(
                                v, (int, float)):
                            val = v
                            break
                return j, val

            # 先拿全院基准（u001 院长，无围栏）
            j_all, v_all = total_of("u001")
            check("H14 全院基准可出数（67653 级别的对照基准）",
                  j_all.get("status") == "answered" and v_all, f"{v_all}")

            s, b, _ = request(port, "/api/security/grants?user_id=u005", method="POST",
                              body={"user_id": "u002", "role": "科室主任",
                                    "dept_scope": ["010511"], "valid_days": 30,
                                    "ref": "HRP-2026-10", "reason": "组织架构同步"})
            check("H14a 管理员按科室授权成功（含依据与有效期）",
                  s == 200 and json.loads(b)["ok"], b[:70])
            j_dept, v_dept = total_of("u002")
            check("H14b ★ 科室级授权的人**能出数**（此前会因 RLS 列不存在而报错）",
                  j_dept.get("status") == "answered" and v_dept is not None,
                  f"status={j_dept.get('status')} val={v_dept}")
            check("H14c ★ RLS 真的收口了：科室数 < 全院数（不是把全院数给了他）",
                  v_dept and v_all and 0 < v_dept < v_all, f"{v_dept} < {v_all}")

            # H14d：用「能看 SQL」的角色验证注入的列名是**物理列**，不是逻辑名
            s, b, _ = request(port, "/api/security/grants?user_id=u005", method="POST",
                              body={"user_id": "E900", "role": "信息科",
                                    "dept_scope": ["010511"], "valid_days": 30,
                                    "ref": "WF-2026-777", "reason": "排障用"})
            j_sql, _ = total_of("E900")
            sql = j_sql.get("sql") or ""
            check("H14d SQL 里的围栏用的是**该数据源真实存在的列**"
                  "（ADS 上应为 discharge_dept_code）",
                  "discharge_dept_code IN ('010511')" in sql
                  and "belong_dept_code" not in sql,
                  [ln.strip() for ln in sql.splitlines() if "010511" in ln][:1])

            # H15：改台账 → 数值与 SQL 立刻跟着变（无缓存残留）
            s, b, _ = request(port, "/api/security/grants?user_id=u005", method="POST",
                              body={"user_id": "u002", "role": "科室主任",
                                    "dept_scope": ["010711"], "valid_days": 30,
                                    "ref": "WF-2026-888", "reason": "轮转到另一个科"})
            check("H15 管理员改台账成功", s == 200 and json.loads(b)["ok"], b[:70])
            j_dept2, v_dept2 = total_of("u002")
            check("H15b 改台账后数值立刻变了（授权变更即时生效，不需要重启）",
                  j_dept2.get("status") == "answered" and v_dept2 and v_dept2 != v_dept,
                  f"{v_dept} → {v_dept2}")
            j_sql2, _ = total_of("E900", "2023年出院人次是多少")
            check("H15c 台账改的是 u002，E900（另一条授权）不受影响",
                  "010711" not in (j_sql2.get("sql") or ""))

            # H16：缺少依据 → 拒绝（否则事后答不出"这个权限谁给的"）
            s, b, _ = request(port, "/api/security/grants?user_id=u005", method="POST",
                              body={"user_id": "u004", "role": "财务处",
                                    "dept_scope": ["*"], "ref": ""})
            check("H16 授权不带依据（ref）→ 400", s == 400 and "依据" in b, b[:70])

            # H17：撤销 → 立刻无权限（离职当天就该失效，不该等缓存/重启）
            s, b, _ = request(port, "/api/security/grants/revoke?user_id=u005", method="POST",
                              body={"user_id": "u002", "reason": "离职", "ref": "WF-2026-889"})
            check("H17 撤销授权成功", s == 200 and json.loads(b)["ok"], b[:70])
            s, b, _ = request(port, "/api/ask", method="POST",
                              body={"question": "2023年出院人次是多少", "user_id": "u002"})
            j = json.loads(b)
            check("H17b 撤销后立刻被拒（不依赖缓存过期）",
                  j.get("status") == "denied" and not j.get("rows"), str(j.get("status")))

            # H18：留痕与链（谁改的、改了谁、依据是什么）
            s, b, _ = request(port, "/api/security/grants/log?user_id=u005")
            j = json.loads(b)
            acts = {x["action"] for x in (j.get("log") or [])}
            check("H18 变更留痕覆盖 grant / update / revoke，且链完整",
                  {"grant", "update", "revoke"} <= acts and (j.get("chain") or {}).get("ok"),
                  f"actions={sorted(acts)}")
            check("H18b 留痕里 actor 是管理员工号、grantee 是被改的人（能分清谁下的手）",
                  all(x.get("actor") and x.get("grantee") for x in (j.get("log") or [])[:5]))

            # H19：同步（界面只允许 yaml 源）+ 台账覆盖率
            s, b, _ = request(port, "/api/security/grants/sync?user_id=u005", method="POST",
                              body={"source": "yaml", "ref": "MIGRATE-check-2"})
            j = json.loads(b)
            check("H19 同步预览：dry-run 返回计划且不写库",
                  s == 200 and j.get("applied") is False and len(j.get("plan", [])) == 5,
                  f"plan={len(j.get('plan', []))}")
            s, b, _ = request(port, "/api/security/grants/sync?user_id=u005", method="POST",
                              body={"source": "file", "ref": "X", "file": "var/askdata.db"})
            check("H19b 同步接口拒绝 file 源（否则等于开了个任意文件读取口）",
                  s == 400 and "yaml" in b, b[:80])

            # H20：D 组审计 —— 授权变更要有身份事件（与问数审计分开）
            dbh = _events()
            dbh.settings.meta_db_path = str(db)
            kinds = {e["kind"] for e in dbh.list_auth_events(limit=50)}
            check("H20 授权变更写了身份事件（grant_changed / grant_revoked）",
                  {"grant_changed", "grant_revoked"} <= kinds, str(sorted(kinds)))

            # H21（docs/12 §6.2）：审计要落在**独立库**，且真的纳链 + 有链锚
            import sqlite3 as _sq
            aconn = _sq.connect(str(audit_db))
            aconn.row_factory = _sq.Row
            arows = [dict(r) for r in aconn.execute("SELECT * FROM audit_log")]
            aconn.close()
            check("H21 真实问数的审计写进了独立库（不是业务元数据库）",
                  len(arows) >= 3 and all(r.get("hash") for r in arows)
                  and audit_db != db, f"{len(arows)} 条，库={audit_db.name}")
            mconn = _sq.connect(str(db))
            try:
                legacy_n = mconn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
            except _sq.Error:
                legacy_n = -1
            mconn.close()
            check("H21b 业务元数据库里的 audit_log 表是空的（审计不再与业务同库）",
                  legacy_n == 0, f"meta.audit_log={legacy_n} 条")
            s, b, _ = request(port, "/api/security/audit/verify?user_id=u005")
            vj = json.loads(b)
            check("H21c 链 + 链锚校验通过，且报告密钥指纹",
                  s == 200 and vj.get("ok") is True and vj.get("anchor_ok") is True
                  and len(vj.get("key_id") or "") >= 8,
                  f"checked={vj.get('checked')} anchors={vj.get('anchors')}")
            s, b, _ = request(port, "/api/security/audit/storage?user_id=u005")
            st = json.loads(b)
            check("H21d 审计存储现状可查（路径 / 密钥来源 / 锚数量）——不是埋在配置里",
                  s == 200 and st.get("path") and "key_id" in str(st.get("key_source"))
                  and "anchors" in st, str(st.get("key_source"))[:44])
            s, _b, _h = request(port, "/api/security/audit/seal?user_id=u002", method="POST")
            check("H21e 非管理员不能封链锚（锚是「某时刻库里有 N 条」的证据）", s == 403,
                  f"status={s}")
        finally:
            _s.meta_db_path, _s.authz_from = old_db, old_from
            (_s.audit_path, _s.audit_key_path, _s.audit_anchor_path) = old_audit

    for f in (db, audit_db, audit_key, audit_anchor):
        _cleanup_db(f)


def _cleanup_db(db: Path) -> None:
    """删掉验收用的临时库。

    ★ 要重试：服务进程刚被 terminate，SQLite 可能还在收尾（-wal 回写），
      删早了会留下一个下次跑就"带着上一轮授权"的库 —— 那会让用例时好时坏。
    """
    for _ in range(10):
        left = []
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(db) + suffix).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                left.append(suffix)
        if not left:
            return
        time.sleep(0.3)


def main() -> int:
    print("身份授信与授权台账验收（docs/12 §3 / §4.2 / §7 A·B 组）")
    print("=" * 74)
    httpd = ThreadingHTTPServer(("127.0.0.1", SSO_PORT), _SsoHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        cas_phase()
        oidc_phase()
        ledger_phase()
        local_phase()
        bootstrap_phase()
        a4_phase()
    except Exception as e:
        check(f"验收中断：{type(e).__name__}: {e}", False)
    finally:
        httpd.shutdown()
        # 两个库都再扫一遍：服务进程退出时可能还握着句柄，早删会被"复活"
        for name in ("_authflow_check.db", "_authledger_check.db", "_authlocal_check.db"):
            _cleanup_db(ROOT / "var" / name)

    print("=" * 74)
    print(f"验收结果: {'全部通过' if passed else '存在失败'}（{total} 项）")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
