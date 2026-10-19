# -*- coding: utf-8 -*-
"""本地账号兜底（F6.1 · docs/12 §3.6）。

定位（一句话）：**只补"你是谁"，不碰"你能看什么"**。
    口令只证明"你是这个工号"；权限永远来自 `auth_grant` 台账（`permission.resolve_user`）。
    这样 SSO 联调完成时，切换的只是凭证来源 —— 台账 / 审计 / RLS / 导出水印一行都不用改。

三条纪律，缺一条这套东西就不该上线：

  1) **口令不可逆**：scrypt（stdlib，零新依赖），参数随哈希一起存，日后调参不用改库；
  2) **锁定期内正确口令也拒绝** —— 否则"锁 15 分钟"对爆破毫无意义（攻击者本来就会等）；
  3) **管理员重置发一次性初始口令**，用户首登必须改密 ——
     否则管理员知道别人的口令，"这条数是谁问的"立刻失去意义（等保问的第一句就是这个）。

自测：`python -m app.core.local_accounts --selftest`
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.config import settings
from app.core import meta_db, permission

# scrypt 参数：n=2^14, r=8, p=1 → 单次约 16 MB / 几十毫秒。
# 内网登录频率极低，这个代价换的是"拿到哈希也难跑字典"。
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
DKLEN = 32
MAXMEM = 64 * 1024 * 1024

# 一次性初始口令的字符集：去掉 0/O/1/l/I —— 抄错一个字符就登不上，
# 现场会变成"重置了但还是进不去"，然后开始怀疑系统。
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"

VALID_STATUS = ("active", "disabled", "locked")


# ────────────────────────────────────────────────────────────
# 口令哈希（不可逆）
# ────────────────────────────────────────────────────────────
def hash_password(pwd: str) -> str:
    """`scrypt$n$r$p$salt_b64$hash_b64` —— 参数随哈希存，便于日后升级强度而不迁移。"""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt((pwd or "").encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                        p=SCRYPT_P, dklen=DKLEN, maxmem=MAXMEM)
    return "scrypt${}${}${}${}${}".format(
        SCRYPT_N, SCRYPT_R, SCRYPT_P,
        base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(pwd: str, stored: str) -> bool:
    """常数时间比较；格式不认识一律 False（**不抛异常**，不给"这条记录坏了"当信号）。"""
    try:
        algo, n, r, p, salt_b64, dk_b64 = (stored or "").split("$")
        if algo != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(dk_b64)
        dk = hashlib.scrypt((pwd or "").encode("utf-8"), salt=salt, n=int(n), r=int(r),
                            p=int(p), dklen=len(expect), maxmem=MAXMEM)
    except Exception:
        return False
    return hmac.compare_digest(dk, expect)


SECURITY_FLOOR_LEN = 8      # 口令长度**硬下限**：配置只能往上调，不能往下调


def pwd_min_len() -> int:
    """当前生效的最小口令长度（配置值，但不得低于硬下限 8）。"""
    try:
        cfg = int(settings.local_pwd_min_len or SECURITY_FLOOR_LEN)
    except (TypeError, ValueError):
        cfg = SECURITY_FLOOR_LEN
    return max(cfg, SECURITY_FLOOR_LEN)


def random_password(length: int = 0) -> str:
    n = max(int(length or 0), pwd_min_len(), 16)
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def check_policy(pwd: str, user_id: str = "") -> Tuple[bool, str]:
    """口令策略：**长度优先**，不强制大小写/符号（复杂度规则会把人逼向 Passw0rd!）。

    长度下限 8 位写死在代码里（`SECURITY_FLOOR_LEN`）：`ASK_LOCAL_PWD_MIN_LEN`
    只能把它调高 —— 现场一次误配（比如写成 4）不该让全线口令一起变弱。
    """
    pwd = pwd or ""
    need = pwd_min_len()
    if len(pwd) < need:
        return False, f"口令至少 {need} 位（当前 {len(pwd)} 位）"
    if user_id and user_id.strip().lower() in pwd.lower():
        return False, "口令不能包含工号（工号是公开信息，口令里的工号等于没有口令）"
    return True, ""


def _pwd_expired(row: Dict[str, Any], t: float) -> bool:
    days = int(settings.local_pwd_max_days or 0)
    if days <= 0:
        return False
    upd = float(row.get("pwd_updated_at") or 0)
    return bool(upd) and (t - upd) > days * 86400


# ────────────────────────────────────────────────────────────
# 登录（唯一会写"失败计数/锁定"的地方）
# ────────────────────────────────────────────────────────────
def login(user_id: str, password: str, *, now: Optional[float] = None) -> Dict[str, Any]:
    """校验口令。返回 `{ok, reason, note, ...}`；**任何失败都不区分"账号不存在"**。

    不区分是有意的：区分开就等于把登录页变成工号枚举器。
    """
    uid = (user_id or "").strip()
    t = time.time() if now is None else now
    if not uid or not password:
        return {"ok": False, "reason": "missing", "note": "工号与口令都必须填写"}

    row = meta_db.get_local_user(uid)
    if not row:
        return {"ok": False, "reason": "bad_credentials", "note": "工号或口令不正确"}

    if row.get("expires_at") and t >= float(row["expires_at"]):
        return {"ok": False, "reason": "expired",
                "note": "账号已过期（兜底账号都有有效期），请联系管理员续期"}

    if (row.get("status") or "") in ("disabled", "revoked"):
        return {"ok": False, "reason": "disabled",
                "note": "账号已停用，请联系管理员启用（停用状态下不接受任何口令）"}

    locked_until = row.get("locked_until")
    if locked_until and t < float(locked_until):
        left = int((float(locked_until) - t) / 60) + 1
        return {"ok": False, "reason": "locked",
                "note": f"连续失败已达上限，账号锁定中，请 {left} 分钟后再试"
                        "（锁定期内口令正确也拒绝）"}

    if not verify_password(password, row.get("pwd_hash") or ""):
        failed = int(row.get("failed_count") or 0) + 1
        lock_until = None
        if failed >= int(settings.local_max_fails or 5):
            lock_until = t + max(1, int(settings.local_lock_minutes or 15)) * 60
        meta_db.record_local_failure(uid, failed, lock_until)
        note = "工号或口令不正确"
        if lock_until:
            note = (f"连续失败 {failed} 次，账号已锁定 {settings.local_lock_minutes} 分钟"
                    "（锁定期内口令正确也拒绝）")
        return {"ok": False, "reason": "bad_credentials", "note": note,
                "failed": failed, "locked": bool(lock_until)}

    meta_db.record_local_login(uid, t)
    return {"ok": True, "reason": "", "user_id": uid,
            "name": row.get("name") or uid,
            "must_change": bool(row.get("must_change")),
            "pwd_expired": _pwd_expired(row, t),
            "last_login_at": row.get("last_login_at")}


# ────────────────────────────────────────────────────────────
# 管理端操作（建号 / 重置 / 停用 / 续期 / 解锁）
# ────────────────────────────────────────────────────────────
def _find(uid: str) -> Optional[Dict[str, Any]]:
    return meta_db.get_local_user((uid or "").strip())


# ────────────────────────────────────────────────────────────
# 首次初始化（账号表为空时的**界面**入口）
# ────────────────────────────────────────────────────────────
#   为什么需要它：`--create-admin` 只能从命令行破局，而现场往往没有命令行权限
#   （交付的是 exe / 容器），于是"账号表空了"就等于"谁都进不去，也没人能建"。
#   一条硬约束：**只在账号表为空时可调用**，建完第一个人立刻关闭 ——
#   否则它就是一个"任何人可创建管理员"的洞。
_BOOTSTRAP_LOCK = threading.Lock()

BOOTSTRAP_REASON = "首次初始化：系统内无账号，创建管理员"


def bootstrap_state() -> Dict[str, Any]:
    """初始化入口的现状（**公开可读**：登录页据此决定渲染登录还是初始化表单）。"""
    total = meta_db.count_local_users()
    mode = (settings.auth_mode or "demo").strip().lower()
    enabled = (settings.auth_local or "off").strip().lower() in ("primary", "breakglass")
    return {
        "needs_bootstrap": total == 0,
        "local_users": total,
        "admin_role": permission.default_admin_role(),
        "local_login_enabled": enabled,
        "auth_mode": mode,
        "pwd_min_len": pwd_min_len(),
        "note": ("系统内还没有任何账号：请创建第一个管理员账号（角色取自 roles.yaml 的 "
                 "admin 标记），建好后该入口自动关闭"
                 if total == 0 else "系统已有账号，初始化入口已关闭"),
    }


def create_first_admin(user_id: str, name: str, password: str,
                       password2: Optional[str] = None, *,
                       valid_days: float = 365,
                       ip: Optional[str] = None) -> Dict[str, Any]:
    """创建**第一个**管理员账号（建号 + 写授权台账，一步做完）。

    与 `--create-admin` 的差别：口令由本人当场设置（不是一次性初始口令），
    因此 `must_change=0` —— 账号的唯一持有者就是设置口令的这个人。
    """
    uid = (user_id or "").strip()
    if not uid:
        return {"ok": False, "error": "工号必填"}
    if not (name or "").strip():
        return {"ok": False, "error": "姓名必填"}
    if password2 is not None and (password or "") != (password2 or ""):
        return {"ok": False, "error": "两次输入的口令不一致"}
    ok, why = check_policy(password or "", uid)
    if not ok:
        return {"ok": False, "error": why}

    # ★ 整个"判空 + 建号"必须串行：并发两次请求都读到"0 个账号"时，
    #   第二个也会建成功 —— 那等于初始化入口能被用两次。
    with _BOOTSTRAP_LOCK:
        if meta_db.count_local_users() > 0:
            return {"ok": False,
                    "error": "初始化入口已关闭（系统已有账号），请用已有管理员账号登录后建号"}
        meta_db.create_local_user(
            uid, name.strip(), hash_password(password), status="active",
            created_by="bootstrap", reason=BOOTSTRAP_REASON,
            expires_at=time.time() + float(valid_days or 365) * 86400, must_change=0)
        meta_db.update_local_user_meta(uid, enabled_by="bootstrap")

    role = permission.default_admin_role()
    from app.core import authz_store
    g = authz_store.upsert_grant(
        uid, role, dept_scope=["*"], campus_scope=["*"], source="manual",
        source_ref="首次初始化", actor="bootstrap",
        reason="首次初始化：创建管理员账号")
    if not g.get("ok"):
        # 账号建了但没授权 → 登录进来也是"未授权"，界面会显示"有账号没权限"。
        # 宁可回滚，也不要留下一个说不清的半成品状态（初始化入口重新打开）。
        meta_db.delete_local_users([uid])
        meta_db.log_auth_event("bootstrap_admin_failed", uid, settings.auth_mode, "local",
                               f"建号成功但授权失败：{g.get('error')}", ip)
        return {"ok": False, "error": f"授权写入失败：{g.get('error')}"}

    meta_db.log_auth_event("bootstrap_admin", uid, settings.auth_mode, "local",
                           f"系统内无账号，首次初始化创建管理员；角色 {role}", ip)
    return {"ok": True, "user_id": uid, "name": name.strip(), "role": role,
            "must_change": False, "grant_id": g.get("grant_id"),
            "note": f"已创建管理员账号（角色 {role}），并写入授权台账（全院范围）"}


def purge_users(keep: Iterable[str] = (), *, actor: str = "cli",
                reason: str = "") -> Dict[str, Any]:
    """**删除**本地账号（维护 / 交付前清理用；日常下线请用 `set_status`）。

    会一并删掉：口令历史、该工号的授权台账行、该工号的全部会话。
    审计与授权变更留痕不动 —— 它们记录的是"发生过什么"，不随账号消失。
    清空后 `bootstrap_state()['needs_bootstrap']` 会变回 True，界面重新出现初始化入口。
    """
    keep_list = [str(k).strip() for k in (keep or ()) if str(k).strip()]
    dropped = meta_db.delete_local_users(keep=keep_list)
    grants = meta_db.delete_grants_for(dropped)
    meta_db.log_auth_event("local_users_purged", actor, settings.auth_mode, "maintenance",
                           f"{actor} 删除本地账号 {len(dropped)} 个：{', '.join(dropped) or '-'}；"
                           f"依据={reason or '-'}", None)
    return {"ok": True, "deleted": dropped, "deleted_count": len(dropped),
            "deleted_grants": int(grants), "kept": keep_list,
            "needs_bootstrap": meta_db.count_local_users() == 0}


def create_user(user_id: str, name: str, *, actor: str, reason: str,
                password: Optional[str] = None, valid_days: Optional[float] = None,
                enabled: bool = False) -> Dict[str, Any]:
    """建号。**依据必填**（与台账同一条纪律：半年后要能回答"这个账号是谁批的"）。"""
    uid = (user_id or "").strip()
    if not uid:
        return {"ok": False, "error": "工号必填"}
    if not (name or "").strip():
        return {"ok": False, "error": "姓名必填"}
    if not (reason or "").strip():
        return {"ok": False,
                "error": "建号依据必填（组织架构文号 / 工单号 / 备案号）——"
                         "没有依据的账号，半年后没人说得清为什么它在"}
    if _find(uid):
        return {"ok": False,
                "error": f"本地账号 {uid} 已存在；重置口令请用 /local/users/password"}

    pwd = password or random_password()
    ok, why = check_policy(pwd, uid)
    if not ok:
        return {"ok": False, "error": why}

    expires_at = None
    if valid_days:
        expires_at = time.time() + float(valid_days) * 86400
    status = "active" if enabled else "disabled"
    meta_db.create_local_user(uid, name.strip(), hash_password(pwd), status=status,
                              created_by=actor, reason=reason.strip(),
                              expires_at=expires_at, must_change=1)
    if enabled:
        meta_db.update_local_user_meta(uid, enabled_by=actor)
    return {"ok": True, "user_id": uid, "status": status, "expires_at": expires_at,
            "initial_password": pwd, "must_change": True,
            "note": "初始口令只显示这一次；用户首次登录必须改密（管理员此后无从得知）"}


def reset_password(user_id: str, *, actor: str, reason: str = "") -> Dict[str, Any]:
    """重置口令：一次性初始口令 + 强制改密 + **撤销该人全部会话**。

    撤销会话不是可选项：只改口令不踢人，等于"口令换了，但偷到旧会话的人还能用 8 小时"。
    """
    uid = (user_id or "").strip()
    row = _find(uid)
    if not row:
        return {"ok": False, "error": f"本地账号 {uid} 不存在"}
    pwd = random_password()
    meta_db.set_local_user_password(uid, hash_password(pwd), must_change=1,
                                    keep=int(settings.local_pwd_history or 0))
    revoked = meta_db.revoke_all_user_sessions(uid, f"pwd_reset_by:{actor}")
    return {"ok": True, "user_id": uid, "initial_password": pwd, "must_change": True,
            "revoked_sessions": revoked,
            "note": "初始口令只显示这一次；旧会话已全部失效"}


def change_password(user_id: str, old_password: str, new_password: str) -> Dict[str, Any]:
    """本人改密（仅改自己的；`must_change=1` 时这是唯一能做的操作）。"""
    uid = (user_id or "").strip()
    row = _find(uid)
    if not row:
        return {"ok": False, "error": "账号不存在"}
    if (row.get("status") or "") in ("disabled", "revoked"):
        return {"ok": False, "error": "账号已停用，无法改密"}
    if not verify_password(old_password, row.get("pwd_hash") or ""):
        return {"ok": False, "error": "原口令不正确"}

    ok, why = check_policy(new_password, uid)
    if not ok:
        return {"ok": False, "error": why}

    # 禁止复用最近 N 次：哈希加盐后无法直接比字符串，只能逐条验
    keep = int(settings.local_pwd_history or 0)
    if keep > 0:
        for h in meta_db.list_pwd_history(uid, keep):
            if verify_password(new_password, h):
                return {"ok": False,
                        "error": f"不能复用最近 {keep} 次用过的口令（换一个，别在末尾加数字）"}

    meta_db.set_local_user_password(uid, hash_password(new_password), must_change=0,
                                    keep=keep)
    return {"ok": True, "user_id": uid, "must_change": False,
            "note": "口令已更新；其他设备上的会话不受影响，如需全部下线请在管理端撤销会话"}


def set_status(user_id: str, status: str, *, actor: str,
               reason: str = "") -> Dict[str, Any]:
    """启用 / 停用。停用会**立刻踢掉全部会话**（不等会话过期）。"""
    uid = (user_id or "").strip()
    row = _find(uid)
    if not row:
        return {"ok": False, "error": f"本地账号 {uid} 不存在"}
    if status not in ("active", "disabled"):
        return {"ok": False, "error": "status 只能是 active / disabled"}

    if status == "active":
        if row.get("expires_at") and float(row["expires_at"]) <= time.time():
            return {"ok": False,
                    "error": "账号已过期：请先续期（expires_at），再启用 —— "
                             "否则会出现'启用了但登不上'"}
        meta_db.update_local_user_meta(uid, status="active", enabled_by=actor,
                                       reason=reason or None)
        return {"ok": True, "user_id": uid, "status": "active",
                "self_enabled": bool(row.get("created_by") and
                                     row.get("created_by") == actor),
                "note": "已启用；该账号权限仍按授权台账解析，启用不等于授权"}

    meta_db.update_local_user_meta(uid, status="disabled", reason=reason or None)
    revoked = meta_db.revoke_all_user_sessions(uid, f"disabled_by:{actor}")
    return {"ok": True, "user_id": uid, "status": "disabled",
            "revoked_sessions": revoked,
            "note": "已停用；其台账授权仍保留（审计要能回答'他曾经有什么'）"}


def renew(user_id: str, *, actor: str, valid_days: float = 365,
          reason: str = "") -> Dict[str, Any]:
    """续期（兜底账号也必须有失效日，续期是一次显式动作）。"""
    uid = (user_id or "").strip()
    if not _find(uid):
        return {"ok": False, "error": f"本地账号 {uid} 不存在"}
    expires_at = time.time() + float(valid_days) * 86400
    meta_db.update_local_user_meta(uid, expires_at=expires_at, reason=reason or None)
    return {"ok": True, "user_id": uid, "expires_at": expires_at}


def unlock(user_id: str, *, actor: str) -> Dict[str, Any]:
    """解锁（管理员确认过身份后再解，不要做"自动解锁"）。"""
    uid = (user_id or "").strip()
    row = _find(uid)
    if not row:
        return {"ok": False, "error": f"本地账号 {uid} 不存在"}
    meta_db.record_local_login(uid, time.time())      # 清 failed_count / locked_until
    if (row.get("status") or "") == "locked":
        meta_db.update_local_user_meta(uid, status="active")
    return {"ok": True, "user_id": uid, "note": "已解锁；失败计数已清零"}


# ────────────────────────────────────────────────────────────
# 查询与自检
# ────────────────────────────────────────────────────────────
def list_users(include_disabled: bool = True) -> List[Dict[str, Any]]:
    return meta_db.list_local_users(include_disabled=include_disabled)


def stats() -> Dict[str, Any]:
    """启动自检用的现状（**必须在 meta_db.init_db() 之后调用**）。"""
    rows = meta_db.list_local_users(include_disabled=True)
    t = time.time()
    warn_days = max(1, int(settings.authz_expiry_warn_days or 14)) * 86400
    return {
        "mode": (settings.auth_local or "off").lower(),
        "total": len(rows),
        "active": sum(1 for r in rows if r.get("status") == "active"),
        "locked": sum(1 for r in rows if r.get("status") == "locked"),
        "must_change": sum(1 for r in rows if r.get("must_change")),
        "expiring_soon": [r["user_id"] for r in rows
                          if r.get("expires_at")
                          and 0 < float(r["expires_at"]) - t <= warn_days],
        "expired": [r["user_id"] for r in rows
                    if r.get("expires_at") and float(r["expires_at"]) <= t],
        # 「创建人自己启用」不是硬约束（单管理员环境会卡死），但值得在启动日志里点名
        "self_enabled": [r["user_id"] for r in rows
                         if r.get("created_by") and r.get("enabled_by")
                         and r["created_by"] == r["enabled_by"]],
    }


# ────────────────────────────────────────────────────────────
# 自测：`python -m app.core.local_accounts --selftest`
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    import tempfile
    from pathlib import Path

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    print("本地账号自测（docs/12 §3.6）")
    old = (settings.meta_db_path, settings.auth_local, settings.local_max_fails,
           settings.local_lock_minutes, settings.local_pwd_history)
    tmp = Path(tempfile.gettempdir()) / f"ask_local_selftest_{int(time.time() * 1000)}.db"
    settings.meta_db_path = str(tmp)            # 自测绝不碰真实库
    settings.auth_local = "primary"
    settings.local_max_fails = 3                # 缩短用例，参数本身在真实环境可配
    settings.local_lock_minutes = 15
    settings.local_pwd_history = 3
    try:
        from app.core import session as session_store

        t0 = time.time()

        # ① 哈希：不可逆、前缀可识别、同一口令两次哈希不同（加盐）
        h1 = hash_password("Med@Hospital2026")
        check("哈希不可逆（不出现在结果里）", "Med@Hospital2026" not in h1)
        check("哈希带算法前缀（便于日后升级参数）", h1.startswith("scrypt$"))
        check("同一口令两次哈希不同（加盐）", h1 != hash_password("Med@Hospital2026"))
        check("正确口令验过", verify_password("Med@Hospital2026", h1))
        check("错误口令不通过", not verify_password("Med@Hospital2027", h1))
        check("哈希被截断/格式坏 → 不通过且不抛异常",
              not verify_password("x", "scrypt$bad") and not verify_password("x", ""))

        # ② 策略：长度优先，但禁止包含工号
        check("太短 → 拒绝", not check_policy("short", "u001")[0])
        check("长度够但含工号 → 拒绝", not check_policy("u001" + "a" * 12, "u001")[0])
        check("长度够且不含工号 → 通过", check_policy("Oncology@2026xyz", "u001")[0])
        check("一次性初始口令满足策略（可直接使用）",
              len(random_password()) >= pwd_min_len())
        # ★ 8 位是**硬下限**：`ASK_LOCAL_PWD_MIN_LEN` 只能调高。现场一次误配（写成 4）
        #   不该让全线口令一起变弱 —— 这条断言就是防"配置能改小"的。
        old_min = settings.local_pwd_min_len
        settings.local_pwd_min_len = 4
        try:
            check("配置把下限调到 4 位 → 仍按 8 位校验",
                  (not check_policy("abcd123", "u001")[0])
                  and check_policy("abcd1234", "u001")[0])
        finally:
            settings.local_pwd_min_len = old_min

        # ③ 建号：依据必填；默认 disabled（兜底账号不许"建好就能用"）
        r = create_user("emg001", "应急账号", actor="u005", reason="")
        check("建号不填依据 → 拒绝", (not r["ok"]) and "依据" in r["error"])
        r = create_user("emg001", "应急账号", actor="u005",
                        reason="信息科备案 2026-10-17", valid_days=30)
        check("建号成功并返回一次性口令", r["ok"] and r["initial_password"],
              "初始口令只出现一次")
        check("新建账号默认停用（启用是显式动作）", r["status"] == "disabled")
        first_pwd = r["initial_password"]
        check("初始口令满足策略", check_policy(first_pwd, "emg001")[0])

        # ④ 停用状态下任何口令都进不去
        r = login("emg001", first_pwd)
        check("停用账号 → 拒绝（原因可读，不是'口令错'）",
              (not r["ok"]) and r["reason"] == "disabled", r["note"])

        # ⑤ 启用后能登录，但 must_change=1
        st = set_status("emg001", "active", actor="u005", reason="今天要用")
        check("启用成功", st["ok"])
        check("创建人自己启用会被标出来（不硬拦，但要在日志里点名）", st["self_enabled"])
        r = login("emg001", first_pwd)
        check("启用后可登录", r["ok"], r.get("note", ""))
        check("首次登录要求改密（管理员不该知道最终口令）", r["must_change"] is True)

        # ⑥ 连续失败锁定 → 锁定期内**正确口令也拒绝**
        for _ in range(settings.local_max_fails):
            login("emg001", "wrong-password")
        r = login("emg001", first_pwd)
        check("锁定期内正确口令也拒绝（否则爆破照样成功）",
              (not r["ok"]) and r["reason"] == "locked", r.get("note", ""))
        un = unlock("emg001", actor="u005")
        check("管理员解锁后恢复", un["ok"] and login("emg001", first_pwd)["ok"])

        # ⑦ 改密：成功后 must_change 归零，且不能复用旧口令
        r = change_password("emg001", first_pwd, "Oncology@2026xyz")
        check("改密成功", r["ok"], r.get("error", ""))
        check("改密后不再强制改密", r["must_change"] is False)
        r2 = change_password("emg001", "Oncology@2026xyz", first_pwd)
        check("不能复用旧口令（最近 N 次）", (not r2["ok"]) and "复用" in r2["error"],
              r2.get("error", ""))
        r3 = change_password("emg001", "wrong-old", "Another@2026xyz")
        check("原口令错 → 拒绝", not r3["ok"])

        # ⑧ 重置口令：一次性口令 + 旧会话全部失效
        sid = session_store.create("emg001", "信息科", auth_source="local")
        r = reset_password("emg001", actor="u005", reason="用户忘了口令")
        check("重置返回一次性口令且要求改密", r["ok"] and r["initial_password"]
              and r["must_change"])
        check("重置会撤销该人全部会话（否则旧 Cookie 还能用）",
              r["revoked_sessions"] >= 1 and
              session_store.resolve(sid["token"])["ok"] is False)
        check("旧口令立即失效", not login("emg001", "Oncology@2026xyz")["ok"])
        check("一次性口令可登录", login("emg001", r["initial_password"])["ok"])

        # ⑨ 停用：立刻踢会话
        sid2 = session_store.create("emg001", "信息科", auth_source="local")
        st = set_status("emg001", "disabled", actor="u005", reason="离职")
        check("停用会撤销全部会话", st["revoked_sessions"] >= 1 and
              session_store.resolve(sid2["token"])["ok"] is False)
        check("停用后拒绝登录", not login("emg001", r["initial_password"])["ok"])

        # ⑩ 过期：兜底账号必须有失效日
        meta_db.update_local_user_meta("emg001", expires_at=t0 - 1)
        check("过期账号拒绝登录", login("emg001", r["initial_password"])["reason"] == "expired")
        check("过期账号不许直接启用（要先续期）",
              (not set_status("emg001", "active", actor="u005")["ok"]))

        # ⑪ 清单里没有哈希；统计口径可用于启动自检
        rows = list_users()
        check("账号清单不含 pwd_hash", all("pwd_hash" not in x for x in rows))
        s = stats()
        check("统计可用（active / must_change / expired）",
              s["total"] >= 1 and isinstance(s["must_change"], int) and s["mode"] == "primary")
        check("续期后可启用", renew("emg001", actor="u005", valid_days=30)["ok"] and
              set_status("emg001", "active", actor="u006")["ok"])
    finally:
        (settings.meta_db_path, settings.auth_local, settings.local_max_fails,
         settings.local_lock_minutes, settings.local_pwd_history) = old
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass

    print("=" * 60)
    print("自测结果: " + ("全部通过" if passed else "存在失败"))
    return passed


def _cli_create_admin(argv: List[str]) -> int:
    """CLI 自举：建第一个本地管理员（命令行破局入口）。

    为什么必须留一个命令行入口：`ASK_AUTHZ_FROM=table` 下台账为空时没人有管理角色，
    `ASK_AUTH_LOCAL=primary` 下没有本地账号时没人能登录 —— 两个"鸡生蛋"叠在一起，
    只能从命令行破局（与 `scripts/sync_authz.py` 首启迁移同一个道理）。
    ★ v0.22 起界面也有等价入口（`/api/security/bootstrap/admin`，仅账号表为空时可用）：
      交付物是 exe / 容器、现场没有命令行时走那条。两者建的账号效果一致，
      差别只是命令行发**一次性初始口令**（首登必须改密），界面由本人当场设口令。
    """
    import argparse

    ap = argparse.ArgumentParser(
        description="建一个本地应急管理员：建号 + 授权（台账）+ 打印一次性初始口令")
    ap.add_argument("--create-admin", metavar="USER_ID", required=True)
    ap.add_argument("--name", default="应急管理员")
    ap.add_argument("--reason", required=True, help="建号依据（备案号 / 工单号），必填")
    ap.add_argument("--role", default="信息科", help="角色码，必须存在于 roles.yaml")
    ap.add_argument("--days", type=float, default=365, help="账号有效期（天）")
    ap.add_argument("--campus", default="*", help="院区范围，'*' = 全院区")
    a = ap.parse_args(argv)

    res = create_user(a.create_admin, a.name, actor="cli", reason=a.reason,
                      valid_days=a.days, enabled=True)
    if not res.get("ok"):
        print("[FAIL] " + str(res.get("error")))
        return 1

    from app.core import authz_store
    g = authz_store.upsert_grant(
        a.create_admin, a.role, dept_scope=["*"],
        campus_scope=[a.campus] if a.campus else None,
        source="manual", source_ref=a.reason, actor="cli",
        reason="本地应急管理员自举（CLI）")

    print("=" * 62)
    print(f"已建本地账号：{a.create_admin}（{a.name}）")
    print(f"一次性初始口令：{res['initial_password']}")
    print("   ★ 只显示这一次；登录后系统强制改密，之后管理员也无从得知")
    print(f"授权：{a.role} / 科室 ['*'] / 院区 "
          f"{[a.campus] if a.campus else '未启用围栏'} → "
          f"{'OK' if g.get('ok') else g.get('error')}")
    print("下一步：起服务 → 用该口令登录 → 强制改密 → 管理端「本地账号」建其他人")
    return 0


def _cli_create_user(argv: List[str]) -> int:
    """CLI 建普通账号（非管理员）。与 `--create-admin` 的差别只有角色与是否 `admin`。

    ★ 建号与授权**一步做完**：只建账号不写台账，登录进来也是 guest（无科室范围），
      现场会以为是"登录坏了"，实际是"没授权"。
    """
    import argparse

    ap = argparse.ArgumentParser(description="建一个本地普通账号（含台账授权）")
    ap.add_argument("--create-user", metavar="USER_ID", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--reason", required=True, help="建号依据（备案号 / 工单号），必填")
    ap.add_argument("--role", default="医务处", help="角色码，必须存在于 roles.yaml")
    ap.add_argument("--dept", default="*", help="科室范围，逗号分隔；'*' = 全院")
    ap.add_argument("--campus", default="*", help="院区范围；'*' = 全院区")
    ap.add_argument("--days", type=float, default=365, help="账号有效期（天）")
    ap.add_argument("--enable", action="store_true",
                    help="建好即启用（不给则保持停用，需在管理端显式启用）")
    a = ap.parse_args(argv)

    dept = [x.strip() for x in (a.dept or "").split(",") if x.strip()] or []
    res = create_user(a.create_user, a.name, actor="cli", reason=a.reason,
                      valid_days=a.days, enabled=bool(a.enable))
    if not res.get("ok"):
        print("[FAIL] " + str(res.get("error")))
        return 1

    from app.core import authz_store
    g = authz_store.upsert_grant(
        a.create_user, a.role, dept_scope=dept,
        campus_scope=[a.campus] if a.campus else None,
        source="manual", source_ref=a.reason, actor="cli",
        reason="本地账号初始化（CLI）")

    print("=" * 62)
    print(f"已建本地账号：{a.create_user}（{a.name}）")
    print(f"一次性初始口令：{res['initial_password']}")
    print("   ★ 只显示这一次；首次登录会被强制改密")
    print(f"账号状态：{res['status']}｜授权：{a.role} / 科室 {dept} / 院区 "
          f"{[a.campus] if a.campus else '未启用围栏'} → "
          f"{'OK' if g.get('ok') else g.get('error')}")
    if res["status"] != "active":
        print("   ⚠ 账号当前是停用状态：管理端「本地账号」里启用后才能登录")
    return 0


def _cli_purge_users(argv: List[str]) -> int:
    """CLI 清理本地账号（交付前 / 演示环境回到"空系统"）。

    ★ 必须显式给出确认短语：这是**删除**，不是停用 —— 账号是审计的归属键，
      删掉之后再问"这条数是谁问的"就答不出来了。日常下线请用管理端「停用」。
    """
    import argparse

    ap = argparse.ArgumentParser(description="删除本地账号（会一并删授权台账行与会话）")
    ap.add_argument("--purge-users", action="store_true", required=True)
    ap.add_argument("--keep", default="", help="保留的工号（逗号分隔），一般留当前管理员")
    ap.add_argument("--reason", default="", help="清理依据（记入身份事件留痕）")
    ap.add_argument("--confirm", required=True,
                    help='确认短语：我确认清理本地账号')
    a = ap.parse_args(argv)
    if (a.confirm or "").strip() != "我确认清理本地账号":
        print('[FAIL] 确认短语不正确。请传 --confirm "我确认清理本地账号"')
        return 1
    keep = [x.strip() for x in (a.keep or "").split(",") if x.strip()]
    res = purge_users(keep=keep, actor="cli", reason=a.reason)
    print("=" * 62)
    print(f"已删除本地账号 {res['deleted_count']} 个：{', '.join(res['deleted']) or '-'}")
    print(f"已删除授权台账行 {res['deleted_grants']} 条｜保留：{', '.join(keep) or '-'}")
    if res["needs_bootstrap"]:
        print("系统当前**没有任何账号**：打开客户端首页会显示「创建管理员账号」。")
        print("（也可以继续用 `--create-admin` 从命令行自举）")
    return 0


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    if "--create-admin" in sys.argv:
        sys.exit(_cli_create_admin(sys.argv[1:]))
    if "--create-user" in sys.argv:
        sys.exit(_cli_create_user(sys.argv[1:]))
    if "--purge-users" in sys.argv:
        sys.exit(_cli_purge_users(sys.argv[1:]))
    print(__doc__)
