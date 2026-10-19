# -*- coding: utf-8 -*-
"""生产化安全闸门（F6 · docs/12 §3.4 / §7 A4）。

要解决的问题：`ASK_AUTH_MODE=demo` 的定义就是"谁都能切换身份"（见 authz 模块文档），
它**只能**跑在本机演示。此前没有任何机制阻止它被绑到 `0.0.0.0` 带上线 ——
那等于把"院长视角 + 全院范围"直接挂在院区网上，而且界面上一切正常。

两道门，缺一不可：

  1) **启动闸门**（docs/12 §7 A4）：demo 模式下绑定地址不是环回 → 拒绝启动。
     绑定地址来源：`ASK_BIND_HOST` 环境变量 > 启动命令行里的 `--host` > 默认环回。
  2) **请求兜底**：即使有人绕过第 1 道（uvicorn 以编程方式启动、argv 里没有 `--host`、
     或前面还有一层反代），非环回对端在 demo 模式下一律 403。

为什么必须两道：第 1 道给运维**立刻可见的失败**（比"静默提供服务"好得多）；
第 2 道保证"漏了第 1 道"也不至于变成事故 —— 安全边界不能依赖"我们恰好解析到了参数"。

自测：`python -m app.core.security_gate --selftest`
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

# 环回地址的常见写法（含 IPv6 与 Windows 的机器名写法）
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "localhost.localdomain"}

# 逃生口：只有在明知风险且确实需要（如临时联调）时才设它。
# 名字刻意起得刺眼，避免被顺手写进生产 compose 文件里。
ALLOW_REMOTE_ENV = "ASK_DEMO_ALLOW_REMOTE"

REASON_DEMO_REMOTE = (
    "拒绝启动：ASK_AUTH_MODE=demo 只允许绑环回地址（当前绑定 {host}）。\n"
    "  演示模式下身份可被任何人切换，对非本机地址开放等于把全院数据敞开（docs/12 §3.4）。\n"
    "  生产/院内联调请改用 ASK_AUTH_MODE=gateway 或 sso；\n"
    "  若确需临时对远端开放（不建议），显式设置 {allow}=1，风险自负。"
)


def _norm(host: Optional[str]) -> str:
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):     # [::1]:8021 这类写法
        h = h[1:-1]
    if h.count(":") == 1 and h.rsplit(":", 1)[1].isdigit():
        h = h.rsplit(":", 1)[0]                   # 127.0.0.1:8021
    return h


def is_loopback(host: Optional[str]) -> bool:
    """是否环回地址。`127.0.0.0/8` 整段都算（不只是 127.0.0.1）。"""
    h = _norm(host)
    if not h:
        return False
    if h in LOOPBACK_HOSTS:
        return True
    if h.startswith("127.") and h.count(".") == 3:
        return True
    # ★ 认不出来的一律**不当作**环回：这里是安全判定，fail-closed 的方向是"更严"。
    return False


def detect_bind_host(argv: Optional[Sequence[str]] = None) -> str:
    """推断服务将绑定的地址。

    优先级：`ASK_BIND_HOST` > 命令行 `--host` / `--host=` > 默认环回。

    为什么读命令行：项目是直接用 `uvicorn app.main:app --host X` 起的，
    应用本身拿不到 uvicorn 的绑定参数；argv 是**唯一**能看出意图的地方。
    读不到就按环回处理 —— 但那只影响第 1 道门，第 2 道门照样按真实对端地址拦。
    """
    env = os.getenv("ASK_BIND_HOST")
    if env and env.strip():
        return env.strip()
    args: List[str] = list(sys.argv if argv is None else argv)
    for i, a in enumerate(args):
        if a == "--host" and i + 1 < len(args):
            return args[i + 1].strip()
        if a.startswith("--host="):
            return a.split("=", 1)[1].strip()
    return "127.0.0.1"


def check_preflight(auth_mode: Optional[str] = None,
                    argv: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """只做判定、不抛异常（供自测与启动日志使用）。"""
    from app.config import settings

    mode = (auth_mode or settings.auth_mode or "demo").strip().lower()
    host = detect_bind_host(argv)
    out: Dict[str, Any] = {"auth_mode": mode, "bind_host": host, "ok": True, "reason": ""}
    if mode == "demo" and not is_loopback(host) and os.getenv(ALLOW_REMOTE_ENV) != "1":
        out["ok"] = False
        out["reason"] = REASON_DEMO_REMOTE.format(host=host, allow=ALLOW_REMOTE_ENV)
    return out


def preflight(auth_mode: Optional[str] = None,
              argv: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """启动闸门：demo + 非环回 → 抛异常，让 uvicorn 起不来（A4 期望"启动失败"）。"""
    r = check_preflight(auth_mode, argv)
    if not r["ok"]:
        raise RuntimeError(r["reason"])
    return r


def demo_remote_reject(client_host: Optional[str],
                       auth_mode: Optional[str] = None) -> Optional[str]:
    """请求级兜底：demo 模式下非环回对端 → 返回拒绝原因，否则 None。"""
    from app.config import settings

    mode = (auth_mode or settings.auth_mode or "demo").strip().lower()
    if mode != "demo":
        return None
    if is_loopback(client_host):
        return None
    if os.getenv(ALLOW_REMOTE_ENV) == "1":
        return None
    return ("演示模式（ASK_AUTH_MODE=demo）只对环回地址提供服务。"
            "当前请求来自非本机地址，已拒绝 —— 生产请改用 ASK_AUTH_MODE=gateway/sso"
            "（docs/12 §3.4）。")


def audit_warnings() -> List[str]:
    """审计存储的启动自检（docs/12 §6.2）。

    ★ 这里有一条**必须喊出来**的运维风险：审计密钥丢了，
      历史链就**永久无法校验**（不是"读不出来"，而是"永远说不清有没有被动过"）。
      它很容易在生产上发生 —— 密钥文件默认自动生成在 `var/` 下，
      清理目录、换机器、容器重建都会把它带走。
    """
    from app.config import settings

    out: List[str] = []
    try:
        from app.core import audit
        st = audit.stats()
        out.append(f"审计库={st['path']}（纳链 {st['chained']} 条 / 遗留 {st['legacy']} 条）"
                   f"｜密钥={st['key_source']}｜链锚={st['anchors']} 个")
        if (settings.audit_hmac_key or "").strip():
            out.append("审计密钥来自环境变量（推荐做法：由信息科与库一起纳入备份）")
        else:
            out.append("⚠️ 审计密钥来自**自动生成的密钥文件**，它丢了历史链就永久无法校验 ——"
                       "请把密钥文件与数据库一起纳入备份，或改用 ASK_AUDIT_HMAC_KEY 由信息科分发")
        if not settings.audit_anchor_every and settings.audit_anchor_path:
            out.append("⚠️ 未开启自动链锚：只靠哈希链**发现不了「删掉尾部的记录」**"
                       "（剩下的链仍然自洽）。建议 ASK_AUDIT_ANCHOR_EVERY 设一个非零值，"
                       "并由运维定期封锚")
        # 锚与库同目录 = 整盘/整目录丢失时两者一起没了，锚的意义被削弱
        try:
            same_dir = (Path(settings.audit_anchor_path).resolve().parent
                        == Path(settings.audit_path).resolve().parent)
            if same_dir:
                out.append("链锚文件与审计库位于**同一目录**：它能挡住「改库的人」，"
                           "但挡不住「整盘/整目录丢失或整体替换」。正式上线建议把锚指到"
                           "另一块盘或另一台机器上的目录（ASK_AUDIT_ANCHOR）")
        except OSError:
            pass
    except Exception as e:
        out.append(f"审计存储自检未完成：{type(e).__name__}: {e}")
    return out


def config_warnings(auth_mode: Optional[str] = None) -> List[str]:
    """启动时的**配置自检**：不是致命错误，但会让身份功能整体不可用。

    为什么必须打印出来：这三项配错的共同表现都是"系统正常运行、就是没人能登录"，
    很容易被当成"院方网络问题"排查很久。启动日志是唯一一次能当场说清的机会。
    """
    from app.config import settings

    mode = (auth_mode or settings.auth_mode or "demo").strip().lower()
    out: List[str] = []
    if mode == "gateway":
        if not settings.trusted_proxies:
            out.append("gateway 模式未配置 ASK_TRUSTED_PROXIES —— 无可信来源，"
                       "**所有请求都会被判为 401**（这比静默接受伪造头安全，但服务等于不可用）")
        else:
            out.append(f"gateway 可信代理名单：{', '.join(settings.trusted_proxies)}"
                       "（名单之外来源注入的同名头会被中间件剥离）")
    if mode == "sso":
        # 复用 identity 的"缺什么"判定，避免两处各写一份配置检查
        try:
            from app.core import identity
            lu = identity.login_url(None)
            if not lu.get("ok"):
                out.append(f"sso 模式（{settings.sso_protocol}）缺少对接参数："
                           f"{', '.join(lu.get('missing') or [])} —— 登录跳转不可用；"
                           f"对接清单见 docs/12 §3.5")
            else:
                out.append(f"sso 模式（{settings.sso_protocol}）登录入口：{lu['url']}")
        except Exception as e:                       # 配置读取失败不该拖垮启动
            out.append(f"sso 配置自检未完成：{type(e).__name__}: {e}")
        if not settings.session_cookie_secure:
            out.append("会话 Cookie 未开 Secure（ASK_SESSION_COOKIE_SECURE=1）；"
                       "内网走 HTTPS 后必须打开")
    if mode == "demo":
        out.append("演示模式：身份可切换、**不是安全边界**，仅限本机（docs/12 §3.4）")
    return out


def rls_warnings(data_source: Optional[str] = None) -> List[str]:
    """行级权限（RLS）相关的启动告警。

    ★ 为什么值得单独一条：RLS 依赖"数据源上真实存在的列"。选了数据源里**没有**的列
      （比如院区列没物化），表现是**启用围栏后所有人都查不出数**，而关掉围栏就一切正常 ——
      现场很容易得出"围栏有问题，先关掉"的结论，于是权限就永久性地消失了。
      把原因写在启动日志里，是这个问题唯一便宜的发现时机。
    """
    from app.config import settings

    ds = (data_source or settings.data_source or "his").lower()
    out: List[str] = []
    try:
        from app.core import permission, physical_map, registry
        pm = physical_map.load_ads() if ds == "ads" else physical_map.load()
        cols = pm.get("columns") or {}
        rls = ((registry.load_registry().get("permission")) or {}).get("rls") or {}
        campus_col = rls.get("campus_column") or "campus_code"
        campus_absent = campus_col in cols and cols.get(campus_col) is None
        cfg = permission.load_roles()

        # ★ 只有「显式限定了某个院区」才会真的注入围栏（["*"] 不注入、[] 在科室层就拒了）。
        #   不区分这一点的话，这条告警会天天误报，最后没人看 ——
        #   而它要提醒的恰恰是"一启用就查不出数"这种最容易被误判为"围栏有问题"的故障。
        def restricts(v) -> bool:
            return bool(v) and "*" not in v

        used_by_users = [uid for uid, u in (cfg.get("users") or {}).items()
                         if restricts((u or {}).get("campus_scope"))]
        used_by_roles = [r for r, v in (cfg.get("roles") or {}).items()
                         if restricts((v or {}).get("default_campus_scope"))]
        if campus_absent and (used_by_users or used_by_roles):
            out.append(
                f"⚠️ 有账号被限定了具体院区（用户 {used_by_users or '—'} / "
                f"角色 {used_by_roles}），但数据源 {ds} 没有物化院区列「{campus_col}」"
                f"（物理映射里声明为 null）——这些账号问数会被直接拒绝"
                f"（报「院区围栏未接线」），现场会表现为「一开围栏就查不出数」"
                f"从而被误判成「围栏坏了」。请先给该数据源补这一列")
    except Exception as e:                       # 自检失败不该拖垮启动
        out.append(f"RLS 配置自检未完成：{type(e).__name__}: {e}")

    # 授权台账的收敛进度（迁移期最该盯的一个数）
    try:
        from app.core import authz_store
        st = authz_store.stats()
        if st["yaml_only_users"]:
            out.append(f"授权台账迁移中：{len(st['yaml_only_users'])} 人仍只在 roles.yaml 里"
                       f"（ASK_AUTHZ_FROM={st['authz_from']}）。"
                       f"注意：严格模式 table 下这些人会变成**无授权**，切档前先用 "
                       f"`python scripts/sync_authz.py --source yaml --ref ...` 迁移")
        if st["expiring_soon"]:
            out.append(f"授权到期提醒：{len(st['expiring_soon'])} 人的授权将在 "
                       f"{settings.authz_expiry_warn_days} 天内到期"
                       f"（{', '.join(st['expiring_soon'][:5])}）—— 临时授权到期前要有人接手")
    except Exception as e:
        out.append(f"授权台账自检未完成：{type(e).__name__}: {e}")
    return out


def local_warnings() -> List[str]:
    """本地账号兜底的启动自检（docs/12 §3.6）。

    ★ 必须在 `meta_db.init_db()` **之后**调用 —— 它要读表。
      这里只报"系统正常跑、但没人能登录 / 没人管"的配错，不重复登录逻辑：
        · 开了本地登录却一个启用中的账号都没有（SSO 也没接 = 谁都进不来）
        · 还有账号是初始口令没改（管理员知道口令，责任无法归到人）
        · breakglass 档位下账号数超上限（兜底通道变成平行账号体系）
    """
    from app.config import settings

    mode = (settings.auth_local or "off").strip().lower()
    if mode not in ("primary", "breakglass"):
        return ["本地登录：未开启（ASK_AUTH_LOCAL=off）—— 只认 SSO / 网关 / 演示身份"]

    out: List[str] = []
    try:
        from app.core import local_accounts
        st = local_accounts.stats()
        out.append(f"本地登录：档位={mode}｜账号 {st['total']} 个"
                   f"（启用 {st['active']} / 锁定 {st['locked']}）")

        if st["active"] == 0:
            if st["total"] == 0:
                # ★ 初始化入口是"先到先得"的：账号表为空期间，任何能看到首页的人
                #   都能创建管理员。这必须写进启动日志，而不是只在界面上轻描淡写。
                out.append("⚠️ 账号表为空：客户端首页会显示「创建管理员账号」，"
                           "建成第一个账号前**任何能访问该页面的人都可以成为管理员** —— "
                           "请立刻完成初始化（或先用 CLI：--create-admin）")
            else:
                out.append("⚠️ 开了本地登录但**没有任何启用中的账号**：若 SSO 也还没接通，"
                           "现场就是「系统正常、没人能登录」。请先建号并启用"
                           "（管理端「本地账号」，或先用 CLI 建一个管理员）")

        if st["must_change"]:
            out.append(f"⚠️ 有 {st['must_change']} 个本地账号仍是**初始口令**未改 —— "
                       f"管理员知道这些口令，问数审计将无法把责任归到人。"
                       f"用户下次登录会被强制改密")

        if mode == "breakglass" and st["active"] > int(settings.local_breakglass_max or 5):
            out.append(f"⚠️ breakglass 档位下启用中的本地账号有 {st['active']} 个，"
                       f"超过建议上限 {settings.local_breakglass_max}：兜底通道到这个程度，"
                       f"就不再是兜底，而是一套没受过 SSO 治理的平行账号")

        if st["expired"]:
            out.append(f"本地账号已过期：{', '.join(st['expired'][:5])}"
                       f"（过期账号无法登录；不自动删除，保留是为了审计可查）")

        if st["expiring_soon"]:
            out.append(f"本地账号到期提醒：{', '.join(st['expiring_soon'][:5])} 将在 "
                       f"{settings.authz_expiry_warn_days} 天内到期")

        if st["self_enabled"]:
            out.append(f"本地账号「创建人自己启用」：{', '.join(st['self_enabled'][:5])} —— "
                       f"建议双人复核（A 建号、B 启用）；单管理员环境可忽略这条")

        # 这是最容易踩的一个组合：开了本地登录却仍用默认的 demo 档，
        # 而 demo 的 resolve() 不读服务端会话 → 页面上根本不会出现登录框。
        if mode in ("primary", "breakglass") and (settings.auth_mode or "").lower() == "demo":
            out.append("⚠️ ASK_AUTH_LOCAL 开了，但 ASK_AUTH_MODE 仍是 demo："
                       "**本地登录在 demo 模式下不生效**（demo 不读服务端会话），"
                       "页面上也不会出现登录框。请把 ASK_AUTH_MODE 设为 sso 或 gateway 后重启")
    except Exception as e:                       # 自检失败不该拖垮启动
        out.append(f"本地账号自检未完成：{type(e).__name__}: {e}")
    return out


# ── 网关注入头的剥离（docs/12 §3.4 gateway 行）──
def should_strip_identity_headers(client_host: Optional[str],
                                  auth_mode: Optional[str] = None,
                                  trusted: Optional[List[str]] = None) -> bool:
    """是否要剥掉本次请求里的身份头。

    ★ 为什么"剥离"是必需的而不是"忽略即可"：应用里读头的代码不止一处，
      任何一处（含将来新增的）直接读 `x-auth-user` 都会变成伪造入口。
      在入口把不可信来源的头**删掉**，是唯一不依赖"每处都记得校验"的做法。
    """
    from app.config import settings
    from app.core import identity

    mode = (auth_mode or settings.auth_mode or "demo").strip().lower()
    if mode == "demo":
        return True                     # demo 不读头，剥掉可避免"以为头生效了"
    if mode == "gateway":
        return not identity.ip_allowed(
            client_host, settings.trusted_proxies if trusted is None else trusted)
    return False                        # sso 模式本就不认头，无需剥离


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    """运行：python -m app.core.security_gate --selftest"""
    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    # ① 环回判定：127.0.0.0/8 整段、IPv6、机器名都算；公网/内网地址都不算
    for h in ("127.0.0.1", "127.0.0.5", "::1", "localhost", "[::1]", "127.0.0.1:8021"):
        check(f"环回判定：{h} 是环回", is_loopback(h))
    for h in ("0.0.0.0", "10.1.33.46", "192.168.1.10", "::", "hospital.local", "", None):
        check(f"环回判定：{h!r} 不是环回", not is_loopback(h))

    # ② 绑定地址推断
    check("detect_bind_host：--host 取值",
          detect_bind_host(["uvicorn", "app.main:app", "--host", "0.0.0.0"]) == "0.0.0.0")
    check("detect_bind_host：--host= 形式",
          detect_bind_host(["uvicorn", "--host=10.0.0.1"]) == "10.0.0.1")
    check("detect_bind_host：没写就是环回（第 1 道门不误拦，第 2 道门兜底）",
          detect_bind_host(["uvicorn", "app.main:app", "--port", "8021"]) == "127.0.0.1")

    # ③ A4：demo + 0.0.0.0 → 必须拒绝启动
    r = check_preflight("demo", ["uvicorn", "--host", "0.0.0.0"])
    check("A4：demo 绑 0.0.0.0 → 拒绝启动", r["ok"] is False, r["reason"].splitlines()[0])
    r2 = check_preflight("demo", ["uvicorn", "--host", "127.0.0.1"])
    check("demo 绑环回 → 放行", r2["ok"] is True)
    r3 = check_preflight("gateway", ["uvicorn", "--host", "0.0.0.0"])
    check("gateway 绑 0.0.0.0 → 放行（生产本就该这么起）", r3["ok"] is True)
    r4 = check_preflight("sso", ["uvicorn", "--host", "10.1.33.46"])
    check("sso 绑院区内网 → 放行", r4["ok"] is True)

    # ④ 请求级兜底：与启动参数无关，只看真实对端
    check("兜底：demo + 远端对端 → 拒绝",
          bool(demo_remote_reject("10.1.33.99", "demo")))
    check("兜底：demo + 环回对端 → 放行", demo_remote_reject("127.0.0.1", "demo") is None)
    check("兜底：gateway 模式不拦（身份由网关注入，本就该对外服务）",
          demo_remote_reject("10.1.33.99", "gateway") is None)

    # ⑤ 逃生口是真的能放行（否则上面那条会显得像"永远拒绝"的假门禁）
    os.environ[ALLOW_REMOTE_ENV] = "1"
    try:
        check("显式 ASK_DEMO_ALLOW_REMOTE=1 → 放开远端",
              check_preflight("demo", ["uvicorn", "--host", "0.0.0.0"])["ok"] is True
              and demo_remote_reject("10.1.33.99", "demo") is None)
    finally:
        os.environ.pop(ALLOW_REMOTE_ENV, None)

    # ⑥ 网关注入头的剥离：不可信来源的头必须"删掉"，不能只是"不读"
    check("剥离：gateway + 来源在白名单 → 不剥离（真网关的头要留）",
          should_strip_identity_headers("10.1.33.9", "gateway", ["10.1.33.0/24"]) is False)
    check("剥离：gateway + 来源不在白名单 → 剥离（否则任何能连上的人都能自称院长）",
          should_strip_identity_headers("10.1.34.9", "gateway", ["10.1.33.0/24"]) is True)
    check("剥离：gateway + 名单为空 → 剥离（fail-closed）",
          should_strip_identity_headers("10.1.33.9", "gateway", []) is True)
    check("剥离：demo 也剥（demo 本就不读头，剥掉免得有人以为头生效了）",
          should_strip_identity_headers("127.0.0.1", "demo", ["127.0.0.1"]) is True)
    check("剥离：sso 不认头，无需剥离",
          should_strip_identity_headers("10.1.33.9", "sso", []) is False)

    # ⑦ 配置自检：三类"系统照常跑、就是没人能登录"的配置错误要在启动日志里说清
    from app.config import settings as _s
    old_mode, old_proxies = _s.auth_mode, _s.trusted_proxies
    try:
        _s.trusted_proxies = []
        ws = config_warnings("gateway")
        check("自检：gateway 无白名单 → 明确提示「所有请求都会 401」",
              any("ASK_TRUSTED_PROXIES" in w and "401" in w for w in ws), str(ws[:1]))
        _s.trusted_proxies = ["10.1.33.0/24"]
        check("自检：gateway 配了白名单 → 打印名单（运维据此核对）",
              any("10.1.33.0/24" in w for w in config_warnings("gateway")))
        old_proto = _s.sso_protocol
        _s.sso_protocol = "cas"
        old_cas, old_svc = _s.cas_base_url, _s.sso_service_url
        _s.cas_base_url, _s.sso_service_url = "", ""
        ws2 = config_warnings("sso")
        check("自检：sso 缺参数 → 列出缺哪几项（而不是等用户点了登录才发现）",
              any("ASK_CAS_BASE" in w for w in ws2), str(ws2[:2]))
        _s.cas_base_url = "https://sso.hospital.local/cas"
        _s.sso_service_url = "https://ask.hospital.local"
        check("自检：sso 配齐后 → 打印登录入口",
              any("sso.hospital.local" in w for w in config_warnings("sso")))
        check("自检：Cookie 未开 Secure 时提示（内网 HTTPS 后必须开）",
              any("Secure" in w for w in config_warnings("sso")))
        _s.cas_base_url, _s.sso_service_url, _s.sso_protocol = old_cas, old_svc, old_proto
    finally:
        _s.auth_mode, _s.trusted_proxies = old_mode, old_proxies

    print("=" * 60)
    print("自测结果: " + ("全部通过" if passed else "存在失败"))
    return passed


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
