# -*- coding: utf-8 -*-
"""本地元数据库（SQLite）。

只存管理与可信类数据，**不存任何业务明细**：
    audit_log        全量审计（F6.5）
    snapshot         口径快照（F2.5）
    feedback         结果反馈（F7.1）
    golden_backlog   问题回流（F7.2）
    caliber_pref     口径偏好（F2.2）
    source_state     源域时效（F8.2）
    metric_version   指标版本留痕（F5.2）
    export_log       导出水印追溯（F4.4）
    chat_session     会话（F1.9，客户端以对话为核心的地基）
    chat_message     会话消息（同上；只存**渲染载荷**，不存业务明细行）
    user_session     身份会话（F6.1，`sso` 模式的服务端会话；登出必须立刻失效）
    auth_event       身份事件留痕（登录/登出/票据校验失败/伪造头被拒，docs/12 §4.4）

★ chat_message 里为什么可以存 payload：那是**已经出数并被审计过**的结果的渲染快照
  （status / 口径条 / 警告 / 反问项 / 截断后的前 N 行），不是新拉的业务数据。
  完整结果在 `snapshot.result_json` 里，回放以快照为准。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    user_id      TEXT NOT NULL,
    role         TEXT NOT NULL,
    question     TEXT NOT NULL,
    status       TEXT NOT NULL,
    intent       TEXT,
    metric_code  TEXT,
    dsl_json     TEXT,
    sql_text     TEXT,
    row_count    INTEGER DEFAULT 0,
    result_hash  TEXT,
    latency_ms   INTEGER DEFAULT 0,
    answer_id    TEXT,
    prev_hash    TEXT,               -- 前一条的 hash（哈希链，docs/12 §6.2）
    hash         TEXT                -- 本条的 sha256；任一字段被改都会断链
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);

CREATE TABLE IF NOT EXISTS snapshot (
    answer_id    TEXT PRIMARY KEY,
    ts           REAL NOT NULL,
    user_id      TEXT,
    question     TEXT,
    metric_code  TEXT,
    metric_ver   TEXT,
    caliber_json TEXT,
    dsl_json     TEXT,
    sql_text     TEXT,
    result_json  TEXT,
    result_hash  TEXT,
    data_as_of   TEXT,
    maturity     TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    answer_id    TEXT,
    user_id      TEXT,
    kind         TEXT NOT NULL,     -- correct / wrong_number / wrong_caliber / unclear
    comment      TEXT,
    question     TEXT,
    handled      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS golden_backlog (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    question     TEXT NOT NULL,
    user_id      TEXT,
    role         TEXT,
    reason       TEXT,
    status       TEXT DEFAULT 'pending'   -- pending / annotated / merged
);

CREATE TABLE IF NOT EXISTS caliber_pref (
    user_id      TEXT NOT NULL,
    scope        TEXT NOT NULL,           -- 指标域，如 ip / op / 或指标码
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   REAL NOT NULL,
    PRIMARY KEY (user_id, scope, key)
);

CREATE TABLE IF NOT EXISTS source_state (
    domain       TEXT PRIMARY KEY,
    source       TEXT,                    -- 所属数据源（F8.1，v0.11）：his / lis / emr ...
    source_table TEXT,
    date_col     TEXT,
    last_data_date TEXT,
    probed_at    REAL,
    freshness    TEXT,                    -- streaming / t_plus_1 / delayed / stale / unconfigured / unknown
    note         TEXT
);

CREATE TABLE IF NOT EXISTS metric_version (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    metric_code  TEXT NOT NULL,
    version      TEXT NOT NULL,
    change_note  TEXT,
    operator     TEXT,
    definition   TEXT
);

CREATE TABLE IF NOT EXISTS export_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    answer_id    TEXT,
    user_id      TEXT,
    fmt          TEXT,
    watermark    TEXT,
    serial       TEXT                -- 导出流水号（含校验位，docs/12 §6.4）
);

-- ── 对话会话（F1.9）：客户端以对话为核心的地基 ──
--   为什么必须有这一层：此前客户端只有「一次问答一个卡片」，
--   多轮指代（「那心内科呢」）无从落地 —— 没有会话就没有「上文」。
CREATE TABLE IF NOT EXISTS chat_session (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    title        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    turn_count   INTEGER DEFAULT 0,
    last_status  TEXT,
    deleted      INTEGER DEFAULT 0   -- 软删除：会话也是数据访问记录，删了要留痕
);
CREATE INDEX IF NOT EXISTS idx_chat_session_user ON chat_session(user_id, updated_at);

CREATE TABLE IF NOT EXISTS chat_message (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL,
    ts           REAL NOT NULL,
    role         TEXT NOT NULL,      -- user / assistant
    user_id      TEXT,
    question     TEXT,
    status       TEXT,
    answer_id    TEXT,
    metric_code  TEXT,
    payload_json TEXT                -- assistant 的渲染载荷（rows 已截断，见 chat 路由）
);
CREATE INDEX IF NOT EXISTS idx_chat_msg_session ON chat_message(session_id, id);

-- ── 身份会话（F6.1 · docs/12 §3.3）──
--   为什么会话必须落到服务端：`sso` 模式下身份来自票据校验，
--   票据只在回调那一次可用；此后每个请求认的是**服务端会话**。
--   只清 Cookie 不算登出 —— Cookie 是客户端的东西，用户随时能恢复旧值，
--   必须同时把服务端这一行置为撤销，且**立即**失效（不等到过期）。
CREATE TABLE IF NOT EXISTS user_session (
    token           TEXT PRIMARY KEY,   -- 随机 32 字节；Cookie 里只放它，不放任何身份字段
    user_id         TEXT NOT NULL,
    role            TEXT,
    auth_mode       TEXT,               -- demo / gateway / sso
    auth_source     TEXT,               -- cas / oidc / gateway / demo（票据从哪来）
    created_at      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    expires_at      REAL NOT NULL,      -- 绝对过期（不可续期）
    idle_expires_at REAL NOT NULL,      -- 空闲过期（每次访问续期，但不超过 expires_at）
    revoked         INTEGER DEFAULT 0,
    revoked_reason  TEXT,
    client_ip       TEXT,
    user_agent      TEXT
);
CREATE INDEX IF NOT EXISTS idx_user_session_user ON user_session(user_id);

-- ── 身份事件留痕（docs/12 §4.4 授权变更留痕 / §6.5 异常访问检测的数据源）──
--   与 audit_log（数据访问）分开：登出/票据失败不是一次"问数"，
--   塞进 audit_log 会让 question 字段出现空串，指标口径统计被污染。
--   只允许追加：本模块不提供 UPDATE/DELETE 接口。
CREATE TABLE IF NOT EXISTS auth_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL,           -- login / logout / login_failed / header_rejected / session_expired
    user_id    TEXT,
    auth_mode  TEXT,
    source     TEXT,
    detail     TEXT,
    client_ip  TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_event_ts ON auth_event(ts);

-- ── 授权台账（F6.2 · docs/12 §4.2「以院内组织架构为准，不建第二套主数据」）──
--   为什么必须从 roles.yaml 搬到这里：
--     roles.yaml 是**代码配置**，改授权要改文件 + 重启 + 走发布流程；
--     而"某人转岗、某人离职"是每天都会发生的事。配置改不动日常，
--     结果就是授权长期不更新 —— 权限系统最危险的状态不是"配错"，是"没人维护"。
--   本产品只维护「映射表 + 同步」：角色能做什么在 roles.yaml（模板），
--   「谁能担任、覆盖哪些科室」在这里，由同步任务从院方组织架构刷入。
CREATE TABLE IF NOT EXISTS auth_grant (
    grant_id     TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,          -- 工号（与身份授信拿到的工号同源）
    role         TEXT NOT NULL,          -- 角色码，必须存在于 roles.yaml 的 roles 段
    dept_scope   TEXT,                   -- JSON 数组：'[]'=显式无授权；'["*"]'=全院
    campus_scope TEXT,                   -- JSON 数组；NULL=未启用院区围栏（≠全院）
    valid_from   REAL,                   -- 生效时间；NULL=立即
    valid_to     REAL,                   -- 失效时间；NULL=长期
    source       TEXT,                   -- hrp / his / yaml / manual（授权从哪来）
    source_ref   TEXT,                   -- ★ 依据：组织架构版本号 / 工单号 / 备案号
    status       TEXT DEFAULT 'active',  -- active / revoked / expired
    synced_at    REAL,
    created_at   REAL,
    updated_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_auth_grant_user ON auth_grant(user_id, status);
CREATE INDEX IF NOT EXISTS idx_auth_grant_role ON auth_grant(role);

-- ── 授权变更留痕（docs/12 §4.4）──
--   ★ actor（谁改的）与 grantee（改了谁）是两个字段：同步任务改的和
--     "某人手动提权"必须能区分开 —— 合成一个字段，审计时就说不出是谁下的手。
--   只允许追加；带哈希链（与 audit_log 同一做法，见 §6.2 的诚实声明）。
CREATE TABLE IF NOT EXISTS auth_grant_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    actor        TEXT,                   -- 操作者工号；同步任务为 'sync:<source>'
    grantee      TEXT,                   -- 被变更的工号
    action       TEXT NOT NULL,          -- grant / update / revoke / remap / expire
    role         TEXT,
    dept_scope   TEXT,
    campus_scope TEXT,
    valid_from   REAL,
    valid_to     REAL,
    source       TEXT,
    source_ref   TEXT,
    reason       TEXT,                   -- 变更原因 / 依据说明
    detail       TEXT,                   -- before → after 的文本描述
    prev_hash    TEXT,
    hash         TEXT
);
CREATE INDEX IF NOT EXISTS idx_auth_grant_log_grantee ON auth_grant_log(grantee, id);

-- ── 本地账号（F6.1 兜底 · docs/12 §3.6）──
--   ★ 本表只回答"你是不是这个工号"，**不存任何权限**：
--     谁能看哪些科室永远来自 auth_grant 台账。两者混在一张表里，
--     SSO 一上线就会出现"两套权限谁生效"的经典事故。
--   ★ pwd_hash 不可逆（scrypt，参数随哈希一起存），口令永不明文落库；
--     admin 重置时发的是**一次性初始口令**，用户首登必须改密。
CREATE TABLE IF NOT EXISTS local_user (
    user_id        TEXT PRIMARY KEY,        -- 工号，与 auth_grant.user_id 同一个键
    name           TEXT NOT NULL,
    pwd_hash       TEXT NOT NULL,           -- scrypt$n$r$p$salt$hash
    pwd_updated_at REAL,
    must_change    INTEGER DEFAULT 1,       -- 建号/重置后强制改密
    status         TEXT DEFAULT 'disabled', -- active / disabled / locked
    failed_count   INTEGER DEFAULT 0,
    locked_until   REAL,
    last_login_at  REAL,
    expires_at     REAL,                    -- 账号有效期（兜底账号也必须有失效日）
    created_by     TEXT,
    created_at     REAL,
    enabled_by     TEXT,                    -- 最近一次启用的操作者（提示"创建人自己启用"）
    reason         TEXT                     -- 建号依据（与台账"依据必填"同一条纪律）
);
CREATE INDEX IF NOT EXISTS idx_local_user_status ON local_user(status);

-- 历史口令（只存哈希，用于"禁止复用最近 N 次"）
CREATE TABLE IF NOT EXISTS local_pwd_history (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  TEXT NOT NULL,
    pwd_hash TEXT NOT NULL,
    ts       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_local_pwd_hist ON local_pwd_history(user_id, id);
"""


def _conn() -> sqlite3.Connection:
    settings.ensure_dirs()
    c = sqlite3.connect(settings.meta_db_path)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    _migrate(c)
    return c


def _migrate(c: sqlite3.Connection) -> None:
    """轻量迁移。

    `CREATE TABLE IF NOT EXISTS` 对**已存在**的表不会加列 —— 新增字段必须显式迁移，
    否则老库上会出现「代码写了、列不存在」的静默失败（只读探测全报 unknown）。
    """
    cols = {r[1] for r in c.execute("PRAGMA table_info(source_state)")}
    if "source" not in cols:
        c.execute("ALTER TABLE source_state ADD COLUMN source TEXT")
    # 审计哈希链（v0.18）：老库补两列。历史上已有的行 hash 为 NULL，
    # verify_chain() 会把它们记为 legacy_rows 并**从第一条有哈希的记录开始**校验 ——
    # 不假装"改造前的记录也被保护了"。
    # 导出流水号（v0.20）：老库补列，历史导出没有号 —— 如实留空，不补造。
    ecols = {r[1] for r in c.execute("PRAGMA table_info(export_log)")}
    if ecols and "serial" not in ecols:
        c.execute("ALTER TABLE export_log ADD COLUMN serial TEXT")
    acols = {r[1] for r in c.execute("PRAGMA table_info(audit_log)")}
    if "prev_hash" not in acols:
        c.execute("ALTER TABLE audit_log ADD COLUMN prev_hash TEXT")
    if "hash" not in acols:
        c.execute("ALTER TABLE audit_log ADD COLUMN hash TEXT")
    # 本地账号（v0.21）：老库补列。`CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，
    # 而这张表在调试期可能已经被建过一次 —— 不补列的话，启用动作会静默写不进去。
    lcols = {r[1] for r in c.execute("PRAGMA table_info(local_user)")}
    if lcols and "enabled_by" not in lcols:
        c.execute("ALTER TABLE local_user ADD COLUMN enabled_by TEXT")


def init_db() -> None:
    with _conn() as c:
        pass


# ── 审计（F6.5）──
#   ★ v0.20 起审计**不再存在本库**：已迁到独立库 `var/audit.db`，
#     由 `app/core/audit.py` 负责（HMAC 链 + 链锚 + 库级只允许追加，见 docs/12 §6.2）。
#     下面三个函数只是**转发**，保留它们是为了不改动 4 个路由的调用点。
#     为什么必须搬走：审计与业务元数据同库时，有库权限的人既能改业务数据也能改审计，
#     "日志没被改过"这句话就无从证明 —— 等保测评不会通过。
def log_audit(user_id: str, role: str, question: str, status: str,
              intent: Optional[str], metric_code: Optional[str],
              dsl: Optional[dict], sql: Optional[str], row_count: int,
              result_hash: Optional[str], latency_ms: int,
              answer_id: Optional[str]) -> int:
    """转发到独立审计库（`app/core/audit.py`）。"""
    from app.core import audit
    return audit.log(user_id, role, question, status, intent, metric_code, dsl, sql,
                     row_count, result_hash, latency_ms, answer_id)


def verify_chain(limit: int = 20000) -> Dict[str, Any]:
    """校验审计链 + 链锚（转发到 `app/core/audit.py`）。

    返回增加：`anchors`（锚数量）/ `anchor_ok` / `anchor_mismatch`（锚对不上的原因）
    —— 只做哈希链**检不出"截断尾部"**（删掉最后几条，剩下的链仍然自洽），
    锚在审计库之外，是这件事的唯一证据。
    """
    from app.core import audit
    return audit.verify_chain(limit)


def list_audit(limit: int = 100, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """审计查询（转发到独立审计库）。"""
    from app.core import audit
    return audit.list_(limit, user_id)


def audit_stats() -> Dict[str, Any]:
    """审计存储现状（管理员用：库路径 / 纳链条数 / 遗留条数 / 密钥来源 / 锚数量）。"""
    from app.core import audit
    return audit.stats()


# ── 身份会话（F6.1 · docs/12 §3.3）──
#   ★ 函数名一律带 user_ 前缀：本模块此前已有「对话会话」的 get_session/list_sessions
#     （F1.9），同名定义在后会把先定义的**静默覆盖**（只表现成"刚写的记录查不到"，
#     自测里就是这么发现的）。
def insert_user_session(row: Dict[str, Any]) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO user_session (token,user_id,role,auth_mode,auth_source,created_at,"
            "last_seen,expires_at,idle_expires_at,revoked,revoked_reason,client_ip,user_agent) "
            "VALUES (:token,:user_id,:role,:auth_mode,:auth_source,:created_at,:last_seen,"
            ":expires_at,:idle_expires_at,0,NULL,:client_ip,:user_agent)", row)


def get_user_session(token: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM user_session WHERE token = ?", (token,)).fetchone()
        return dict(r) if r else None


def touch_user_session(token: str, last_seen: float, idle_expires_at: float) -> None:
    """续期。调用方负责"距上次看到不足 N 秒就跳过"，避免每请求一次写。

    为什么续期也写库：空闲超时必须是**服务端**的判定。只在客户端 Cookie 上
    带时间戳等于让用户自己决定会话是否过期（改一下 Cookie 就能永久在线）。
    """
    with _conn() as c:
        c.execute("UPDATE user_session SET last_seen = ?, idle_expires_at = ? "
                  "WHERE token = ? AND revoked = 0", (last_seen, idle_expires_at, token))


def revoke_user_session(token: str, reason: str = "") -> bool:
    """登出 / 强制下线：**立即**失效，不等过期。

    返回是否真的撤销了一条（已撤销/不存在 → False，供接口如实回显）。
    """
    with _conn() as c:
        cur = c.execute("UPDATE user_session SET revoked = 1, revoked_reason = ? "
                        "WHERE token = ? AND revoked = 0", (reason or "logout", token))
        return cur.rowcount > 0


def revoke_all_user_sessions(user_id: str, reason: str = "",
                             except_token: Optional[str] = None) -> int:
    """把某人的全部会话踢掉（转岗/离职/异常访问）。

    对应 docs/12 §7 B5「离职账号同步后再问 → denied，无需人工干预」的服务端一半。
    """
    sql = "UPDATE user_session SET revoked = 1, revoked_reason = ? WHERE user_id = ? AND revoked = 0"
    args: List[Any] = [reason or "revoked", user_id]
    if except_token:
        sql += " AND token <> ?"
        args.append(except_token)
    with _conn() as c:
        return c.execute(sql, args).rowcount


def purge_user_session(token: str) -> None:
    """物理删除（仅用于测试与过期清理，不用于登出）。"""
    with _conn() as c:
        c.execute("DELETE FROM user_session WHERE token = ?", (token,))


def purge_expired_user_sessions(now: float) -> int:
    """清理已过期/已撤销且超过宽限期的会话行。

    ★ 只清"早就没用了"的行：撤销记录本身是证据，立刻删掉会让
      "这条会话是正常登出还是被踢"无从追溯。宽限期见 session.GRACE_SECONDS。
    """
    with _conn() as c:
        return c.execute(
            "DELETE FROM user_session WHERE (revoked = 1 OR expires_at < ?) "
            "AND expires_at < ?", (now, now - 7 * 24 * 3600)).rowcount


def list_user_sessions(user_id: Optional[str] = None, limit: int = 100,
                       include_revoked: bool = False) -> List[Dict[str, Any]]:
    """在线会话清单（管理端「谁在线」＋ §6.5 异常访问检测）。

    ★ 返回的字段里**没有 token** —— 会话令牌是凭据，不是可展示的数据。
    """
    sql = ("SELECT user_id,role,auth_mode,auth_source,created_at,last_seen,expires_at,"
           "idle_expires_at,revoked,revoked_reason,client_ip FROM user_session")
    where, args = [], []
    if user_id:
        where.append("user_id = ?")
        args.append(user_id)
    if not include_revoked:
        where.append("revoked = 0")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY last_seen DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


# ── 身份事件留痕（docs/12 §4.4 / §6.5）──
def log_auth_event(kind: str, user_id: Optional[str] = None, auth_mode: Optional[str] = None,
                   source: Optional[str] = None, detail: Optional[str] = None,
                   client_ip: Optional[str] = None) -> int:
    """只追加，不提供修改/删除接口。

    为什么与 audit_log 分开：audit_log 的 `question` 是 NOT NULL 且被指标口径统计消费，
    把登出/票据失败塞进去会污染"谁问了多少"这类统计。
    """
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO auth_event (ts,kind,user_id,auth_mode,source,detail,client_ip) "
            "VALUES (?,?,?,?,?,?,?)",
            (time.time(), kind, user_id, auth_mode, source, detail, client_ip))
        return cur.lastrowid


def list_auth_events(limit: int = 100, kind: Optional[str] = None,
                     user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM auth_event"
    where, args = [], []
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if user_id:
        where.append("user_id = ?")
        args.append(user_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


# ── 授权台账（F6.2 · docs/12 §4.2 / §4.4）──
#   本模块只做**存取与留痕**；"什么算有效授权、怎么合并同步"在
#   app/core/authz_store.py（那里才是语义，这里只是表）。
_AUTHZ_HASH_FIELDS = ("ts", "actor", "grantee", "action", "role", "dept_scope",
                      "campus_scope", "valid_from", "valid_to", "source",
                      "source_ref", "reason", "detail")


def authz_hash(row: Dict[str, Any], prev_hash: str) -> str:
    """一条授权变更记录的链式哈希。

    ★ v0.20 起与审计链一样用 **HMAC**（密钥在库外，见 docs/12 §6.2/§4.4）：
      "这批权限是谁给的、什么时候给的"要经得起复查，就不该用一个人人算得出的算法
      放在一个人人能改的库里。域分隔用 `authz-v1`，与审计链的 `v1` 区分开。
    """
    from app.core import audit
    key, _src, _kid = audit._load_key()
    parts = ["" if row.get(k) is None else str(row.get(k)) for k in _AUTHZ_HASH_FIELDS]
    return audit.chain_hash_parts("authz-v1", parts, prev_hash, key)


def log_authz_change(actor: Optional[str], grantee: Optional[str], action: str,
                     role: Optional[str] = None,
                     dept_scope: Optional[str] = None,
                     campus_scope: Optional[str] = None,
                     valid_from: Optional[float] = None, valid_to: Optional[float] = None,
                     source: Optional[str] = None, source_ref: Optional[str] = None,
                     reason: Optional[str] = None, detail: Optional[str] = None) -> int:
    """追加一条授权变更记录。**没有 UPDATE/DELETE 接口** —— 改了谁就是改了谁。"""
    row: Dict[str, Any] = {"ts": time.time(), "actor": actor, "grantee": grantee,
                           "action": action, "role": role, "dept_scope": dept_scope,
                           "campus_scope": campus_scope, "valid_from": valid_from,
                           "valid_to": valid_to, "source": source, "source_ref": source_ref,
                           "reason": reason, "detail": detail}
    with _conn() as c:
        r = c.execute("SELECT hash FROM auth_grant_log WHERE hash IS NOT NULL "
                      "ORDER BY id DESC LIMIT 1").fetchone()
        prev = (r["hash"] if r and r["hash"] else "")
        row["prev_hash"] = prev
        row["hash"] = authz_hash(row, prev)
        cur = c.execute(
            "INSERT INTO auth_grant_log (ts,actor,grantee,action,role,dept_scope,campus_scope,"
            "valid_from,valid_to,source,source_ref,reason,detail,prev_hash,hash) "
            "VALUES (:ts,:actor,:grantee,:action,:role,:dept_scope,:campus_scope,:valid_from,"
            ":valid_to,:source,:source_ref,:reason,:detail,:prev_hash,:hash)", row)
        return cur.lastrowid


def list_authz_log(grantee: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM auth_grant_log"
    args: List[Any] = []
    if grantee:
        sql += " WHERE grantee = ?"
        args.append(grantee)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def verify_authz_chain(limit: int = 5000) -> Dict[str, Any]:
    """校验授权变更链（"这批权限是谁在什么时候给的"要经得起复查）。

    ★ 兼容改造前用裸 sha256 写的行：HMAC 对不上时**再试一次旧算法**，
      对得上就计入 `legacy_algo` 并明确说明"这些行的保护力弱于新链"。
      为什么要这样：直接判失败会让所有老部署一升级就报"授权链断了"，
      而实际上什么都没发生 —— 狼来了几次之后，就没人再看这条告警了。
    """
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM auth_grant_log ORDER BY id ASC LIMIT ?", (limit,))]
    checked = legacy = legacy_algo = 0
    prev = ""
    for row in rows:
        if not row.get("hash"):
            legacy += 1
            continue
        if (row.get("prev_hash") or "") != prev:
            return {"ok": False, "checked": checked, "legacy_rows": legacy,
                    "broken_at": row["id"], "reason": "prev_hash 与前一条记录的 hash 不一致"}
        if authz_hash(row, prev) != row["hash"]:
            parts = ["" if row.get(k) is None else str(row.get(k)) for k in _AUTHZ_HASH_FIELDS]
            old = hashlib.sha256(("|".join(parts) + "|" + prev).encode("utf-8")).hexdigest()
            if old == row["hash"]:
                legacy_algo += 1        # 改造前的行：旧算法仍自洽，如实标注
            else:
                return {"ok": False, "checked": checked, "legacy_rows": legacy,
                        "legacy_algo": legacy_algo, "broken_at": row["id"],
                        "reason": "记录内容与自身哈希不符（该行被改动，或换了审计密钥）"}
        checked += 1
        prev = row["hash"]
    return {"ok": True, "checked": checked, "legacy_rows": legacy,
            "legacy_algo": legacy_algo, "tail_hash": prev,
            "note": ("链用 HMAC 保护，密钥在库外；"
                     f"其中 {legacy_algo} 条是改造前用裸 sha256 写的（保护力较弱，已如实标注）")
            if legacy_algo else "链用 HMAC 保护，密钥在库外（能改库的人也算不出新链）"}


def put_grant(row: Dict[str, Any]) -> None:
    """写入/覆盖一条**授权台账行**（不含留痕，留痕由 authz_store 统一负责）。"""
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO auth_grant (grant_id,user_id,role,dept_scope,campus_scope,"
            "valid_from,valid_to,source,source_ref,status,synced_at,created_at,updated_at) "
            "VALUES (:grant_id,:user_id,:role,:dept_scope,:campus_scope,:valid_from,:valid_to,"
            ":source,:source_ref,:status,:synced_at,:created_at,:updated_at)", row)


def get_grant_row(grant_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM auth_grant WHERE grant_id = ?", (grant_id,)).fetchone()
        return dict(r) if r else None


def list_grant_rows(user_id: Optional[str] = None, status: Optional[str] = None,
                    limit: int = 500) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM auth_grant"
    where, args = [], []
    if user_id:
        where.append("user_id = ?")
        args.append(user_id)
    if status:
        where.append("status = ?")
        args.append(status)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY user_id LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def set_grant_status(grant_id: str, status: str) -> None:
    """只改状态与更新时间。授权内容本身不可原地修改 —— 要改就换一条新行（带留痕）。"""
    with _conn() as c:
        c.execute("UPDATE auth_grant SET status = ?, updated_at = ? WHERE grant_id = ?",
                  (status, time.time(), grant_id))


# ── 快照（F2.5）──
def save_snapshot(snap: Dict[str, Any]) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO snapshot (answer_id,ts,user_id,question,metric_code,"
            "metric_ver,caliber_json,dsl_json,sql_text,result_json,result_hash,data_as_of,maturity)"
            " VALUES (:answer_id,:ts,:user_id,:question,:metric_code,:metric_ver,:caliber_json,"
            ":dsl_json,:sql_text,:result_json,:result_hash,:data_as_of,:maturity)", snap)


def get_snapshot(answer_id: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM snapshot WHERE answer_id = ?", (answer_id,)).fetchone()
        return dict(r) if r else None


def list_snapshots(limit: int = 50, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """快照列表。传 user_id = 只看**本人**的（12 §6.3：历史限本人）。"""
    sql = ("SELECT answer_id,ts,user_id,question,metric_code,maturity,data_as_of "
           "FROM snapshot")
    args: List[Any] = []
    if user_id:
        sql += " WHERE user_id = ?"
        args.append(user_id)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


# ── 反馈（F7.1）──
def save_feedback(answer_id: Optional[str], user_id: str, kind: str,
                  comment: Optional[str], question: Optional[str]) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO feedback (ts,answer_id,user_id,kind,comment,question) VALUES (?,?,?,?,?,?)",
            (time.time(), answer_id, user_id, kind, comment, question))
        return cur.lastrowid


def list_feedback(limit: int = 100) -> List[Dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM feedback ORDER BY ts DESC LIMIT ?", (limit,))]


# ── 回流 Golden 集（F7.2）──
def push_backlog(question: str, user_id: str, role: str, reason: str) -> int:
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO golden_backlog (ts,question,user_id,role,reason) VALUES (?,?,?,?,?)",
            (time.time(), question, user_id, role, reason))
        return cur.lastrowid


def list_backlog(status: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM golden_backlog"
    args: List[Any] = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def export_backlog(limit: int = 200) -> List[Dict[str, Any]]:
    """导出为 Golden 集标注候选（09 规范的 question 来源之一）。"""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT question,user_id,role,reason FROM golden_backlog "
            "WHERE status='pending' ORDER BY ts DESC LIMIT ?", (limit,))]


# ── 口径偏好（F2.2）──
def set_pref(user_id: str, scope: str, key: str, value: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO caliber_pref (user_id,scope,key,value,updated_at)"
            " VALUES (?,?,?,?,?)", (user_id, scope, key, value, time.time()))


def get_prefs(user_id: str, scope: Optional[str] = None) -> Dict[str, str]:
    sql = "SELECT key,value FROM caliber_pref WHERE user_id = ?"
    args: List[Any] = [user_id]
    if scope:
        sql += " AND scope = ?"
        args.append(scope)
    with _conn() as c:
        return {r["key"]: r["value"] for r in c.execute(sql, args)}


def get_prefs_all(user_id: str) -> Dict[str, Dict[str, str]]:
    """该用户的全部口径偏好，按 scope（指标码）分组：{metric_code: {rule_id: 选项文本}}。

    ★ 为什么按指标码分组：口径的可选值是按指标判定的（「开单科室」对门诊收入有效、
      对住院出院人次无效），跨指标沿用会直接踩到"口径用不了"。
    ★ 为什么要有这个函数：`get_prefs(scope=None)` 会把不同 scope 的同名 key 合并，
      拿它当偏好来源就会串指标。
    """
    with _conn() as c:
        rows = list(c.execute(
            "SELECT scope,key,value FROM caliber_pref WHERE user_id = ?", (user_id,)))
    out: Dict[str, Dict[str, str]] = {}
    for r in rows:
        out.setdefault(r["scope"], {})[r["key"]] = r["value"]
    return out


def clear_pref(user_id: str, scope: str, key: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM caliber_pref WHERE user_id=? AND scope=? AND key=?",
                  (user_id, scope, key))


# ── 源域时效（F8.2）──
def upsert_source_state(domain: str, source_table: str, date_col: str,
                        last_data_date: Optional[str], freshness: str,
                        note: str = "", source: str = "his") -> None:
    """写入源域时效。

    source：域所属数据源（v0.11 起记录）。domain 仍是主键 ——
    因此**域名必须跨源唯一**，该约束由 app/core/sources.py 的自测把关。
    """
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO source_state (domain,source,source_table,date_col,"
            "last_data_date,probed_at,freshness,note) VALUES (?,?,?,?,?,?,?,?)",
            (domain, source, source_table, date_col, last_data_date,
             time.time(), freshness, note))


def list_source_state() -> List[Dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM source_state ORDER BY domain")]


def get_source_state(domain: str) -> Optional[Dict[str, Any]]:
    with _conn() as c:
        r = c.execute("SELECT * FROM source_state WHERE domain=?", (domain,)).fetchone()
        return dict(r) if r else None


# ── 指标版本（F5.2）──
def log_metric_version(metric_code: str, version: str, change_note: str,
                       operator: str, definition: str) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO metric_version (ts,metric_code,version,change_note,operator,definition)"
            " VALUES (?,?,?,?,?,?)",
            (time.time(), metric_code, version, change_note, operator, definition))


def list_metric_versions(metric_code: Optional[str] = None,
                         limit: int = 50) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM metric_version"
    args: List[Any] = []
    if metric_code:
        sql += " WHERE metric_code = ?"
        args.append(metric_code)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


# ── 导出追溯（F4.4 · docs/12 §6.4）──
def log_export(answer_id: Optional[str], user_id: str, fmt: str, watermark: str) -> str:
    """记录一次导出，**返回流水号**（调用方把它写进水印）。

    ★ 序号按"当天第几笔"取，落在同一条 INSERT 之前先数一次 ——
      并发导出理论上可能撞号，但这里追求的不是严格排号，而是
      "任意两份文件的编号不同、且能反查到一条记录"；
      撞号的表现是两条记录同号，反查时会看到两条，不会误判成伪造。
    """
    from app.core import export_trace
    now = time.time()
    day_start = now - (now % 86400)          # 以 UTC 日切；仅用于排号，不参与语义
    with _conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM export_log WHERE ts >= ?",
                      (day_start,)).fetchone()["n"]
        serial = export_trace.make_serial(int(n) + 1)
        c.execute("INSERT INTO export_log (ts,answer_id,user_id,fmt,watermark,serial) "
                  "VALUES (?,?,?,?,?,?)", (now, answer_id, user_id, fmt, watermark, serial))
    return serial


def set_export_watermark(serial: str, watermark: str) -> None:
    """把最终水印（含流水号）补记到这条导出记录上。

    为什么分两步（先记流水号、后补水印）：水印里要写流水号，而流水号只能由
    「插入记录」这一刻产生。宁可记录先存在、水印随后补齐，也不要反过来 ——
    反过来会出现"文件里有号、日志里查不到"的窗口，而那正是反查时最尴尬的情况
    （分不清是伪造还是刚导完还没记上）。
    """
    with _conn() as c:
        c.execute("UPDATE export_log SET watermark = ? WHERE serial = ?", (watermark, serial))


def get_exports_by_serial(serial: str) -> List[Dict[str, Any]]:
    """按流水号反查导出记录（0 条 = 我们没导出过这份文件）。"""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM export_log WHERE serial = ? ORDER BY id", (serial,))]


# ── 对话会话（F1.9）──
def create_session(user_id: str, title: str = "") -> str:
    sid = uuid.uuid4().hex[:16]
    now = time.time()
    with _conn() as c:
        c.execute("INSERT INTO chat_session (session_id,user_id,title,created_at,updated_at,"
                  "turn_count,last_status,deleted) VALUES (?,?,?,?,?,0,NULL,0)",
                  (sid, user_id, (title or "新会话")[:60], now, now))
    return sid


def get_session(session_id: str, user_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """取会话。**必须传 user_id 做归属校验**（12 §6.3）：不传只在内部排查用。"""
    sql = "SELECT * FROM chat_session WHERE session_id=? AND deleted=0"
    args: List[Any] = [session_id]
    if user_id is not None:
        sql += " AND user_id=?"
        args.append(user_id)
    with _conn() as c:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None


def list_sessions(user_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT session_id,title,created_at,updated_at,turn_count,last_status "
            "FROM chat_session WHERE user_id=? AND deleted=0 "
            "ORDER BY updated_at DESC LIMIT ?", (user_id, limit))]


def rename_session(session_id: str, user_id: str, title: str) -> bool:
    with _conn() as c:
        cur = c.execute("UPDATE chat_session SET title=?, updated_at=? "
                        "WHERE session_id=? AND user_id=? AND deleted=0",
                        ((title or "新会话")[:60], time.time(), session_id, user_id))
        return cur.rowcount > 0


def delete_session(session_id: str, user_id: str) -> bool:
    """软删除：会话是数据访问记录的一部分，审计口径下不能真删（docs/12 §6）。"""
    with _conn() as c:
        cur = c.execute("UPDATE chat_session SET deleted=1, updated_at=? "
                        "WHERE session_id=? AND user_id=? AND deleted=0",
                        (time.time(), session_id, user_id))
        return cur.rowcount > 0


def append_message(session_id: str, role: str, user_id: str, question: Optional[str] = None,
                   status: Optional[str] = None, answer_id: Optional[str] = None,
                   metric_code: Optional[str] = None,
                   payload: Optional[Dict[str, Any]] = None) -> int:
    """追加一条消息。

    user 消息会顺带推进会话的 turn_count / updated_at / 标题（首问当标题）。
    ★ 标题只在会话还没有名字时写一次：后面每轮都改标题，历史会话列表就没法用了。
    """
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO chat_message (session_id,ts,role,user_id,question,status,answer_id,"
            "metric_code,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (session_id, now, role, user_id, question, status, answer_id, metric_code,
             json.dumps(payload, ensure_ascii=False, default=str) if payload else None))
        if role == "user":
            row = c.execute("SELECT title FROM chat_session WHERE session_id=?",
                            (session_id,)).fetchone()
            title = (row["title"] if row else "") or ""
            if not title or title == "新会话":
                title = (question or "").strip()[:24] or "新会话"
            c.execute("UPDATE chat_session SET updated_at=?, turn_count=turn_count+1, "
                      "title=? WHERE session_id=?", (now, title, session_id))
        elif status:
            c.execute("UPDATE chat_session SET updated_at=?, last_status=? WHERE session_id=?",
                      (now, status, session_id))
        return cur.lastrowid


def list_messages(session_id: str, limit: int = 200) -> List[Dict[str, Any]]:
    """按时间正序返回消息（前端从旧到新铺成对话流）。"""
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM chat_message WHERE session_id=? ORDER BY id ASC LIMIT ?",
            (session_id, limit))]
    for r in rows:
        if r.get("payload_json"):
            try:
                r["payload"] = json.loads(r["payload_json"])
            except Exception:
                r["payload"] = None
        r.pop("payload_json", None)
    return rows


def last_turn_context(session_id: str) -> Optional[Dict[str, Any]]:
    """会话内最近一条**出过 DSL** 的回答 → 供下一轮继承上文（F1.2 指代消解）。

    返回 {"question": 上一问, "dsl": 上一轮实际执行的口径, "metric_code": ...}；
    拿不到有效 DSL 时返回 None —— 宁可让模型从头解析，也不要传一段空上下文假装能指代。
    """
    with _conn() as c:
        a = c.execute("SELECT question,payload_json,status,metric_code FROM chat_message "
                      "WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",
                      (session_id,)).fetchone()
        u = c.execute("SELECT question FROM chat_message WHERE session_id=? AND role='user' "
                      "ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    if not a:
        return None
    try:
        payload = json.loads(a["payload_json"] or "{}")
    except Exception:
        payload = {}
    dsl = payload.get("dsl")
    if not isinstance(dsl, dict) or not dsl:
        return None
    return {"question": (u["question"] if u else a["question"]) or "",
            "dsl": dsl, "metric_code": a["metric_code"] or payload.get("metric_code"),
            "status": a["status"]}


# ── 本地账号（F6.1 兜底 · docs/12 §3.6）──
#   ★ 哈希只在 `get_local_user()` 里出库（校验口令用），对外清单一律**显式列字段**，
#     不写 `SELECT *` + pop —— 显式列字段的好处是"以后有人加了列也不会顺手泄露"。
_LOCAL_PUBLIC_COLS = ("user_id,name,status,must_change,failed_count,locked_until,"
                      "last_login_at,expires_at,created_by,created_at,enabled_by,"
                      "reason,pwd_updated_at")


def get_local_user(user_id: str) -> Optional[Dict[str, Any]]:
    """取一条本地账号（**含** pwd_hash，仅供 local_accounts 校验口令）。"""
    with _conn() as c:
        r = c.execute("SELECT * FROM local_user WHERE user_id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def list_local_users(include_disabled: bool = True) -> List[Dict[str, Any]]:
    """账号清单（**不含** pwd_hash）。"""
    sql = f"SELECT {_LOCAL_PUBLIC_COLS} FROM local_user"
    if not include_disabled:
        sql += " WHERE status='active'"
    sql += " ORDER BY created_at ASC"
    with _conn() as c:
        return [dict(r) for r in c.execute(sql)]


def create_local_user(user_id: str, name: str, pwd_hash: str, *, status: str = "disabled",
                      created_by: str = "", reason: str = "",
                      expires_at: Optional[float] = None,
                      must_change: int = 1) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT INTO local_user (user_id,name,pwd_hash,pwd_updated_at,must_change,status,"
            "failed_count,locked_until,last_login_at,expires_at,created_by,created_at,"
            "enabled_by,reason) VALUES (?,?,?,?,?,?,0,NULL,NULL,?,?,?,NULL,?)",
            (user_id, name, pwd_hash, now, int(must_change), status,
             expires_at, created_by, now, reason))
        # ★ 初始口令也要进历史：否则"改回初始口令"能绕过"禁止复用最近 N 次"。
        c.execute("INSERT INTO local_pwd_history (user_id,pwd_hash,ts) VALUES (?,?,?)",
                  (user_id, pwd_hash, now))


def update_local_user_meta(user_id: str, *, name: Optional[str] = None,
                           status: Optional[str] = None,
                           expires_at: Optional[float] = None,
                           reason: Optional[str] = None,
                           enabled_by: Optional[str] = None) -> None:
    """改元信息：`None` = 保留原值（与接口的合并语义一致，不整条覆盖）。"""
    with _conn() as c:
        c.execute(
            "UPDATE local_user SET name=COALESCE(?,name), status=COALESCE(?,status), "
            "expires_at=COALESCE(?,expires_at), reason=COALESCE(?,reason), "
            "enabled_by=COALESCE(?,enabled_by) WHERE user_id=?",
            (name, status, expires_at, reason, enabled_by, user_id))


def set_local_user_password(user_id: str, pwd_hash: str, *, must_change: int = 0,
                            keep: int = 3) -> None:
    """写新哈希 + 记历史（保留最近 `keep` 条，用于"禁止复用最近 N 次"）。"""
    now = time.time()
    with _conn() as c:
        c.execute("UPDATE local_user SET pwd_hash=?, pwd_updated_at=?, must_change=?, "
                  "failed_count=0, locked_until=NULL WHERE user_id=?",
                  (pwd_hash, now, int(must_change), user_id))
        c.execute("INSERT INTO local_pwd_history (user_id,pwd_hash,ts) VALUES (?,?,?)",
                  (user_id, pwd_hash, now))
        if keep > 0:
            c.execute("DELETE FROM local_pwd_history WHERE user_id=? AND id NOT IN "
                      "(SELECT id FROM local_pwd_history WHERE user_id=? "
                      " ORDER BY id DESC LIMIT ?)", (user_id, user_id, keep))


def list_pwd_history(user_id: str, limit: int = 3) -> List[str]:
    with _conn() as c:
        rows = c.execute("SELECT pwd_hash FROM local_pwd_history WHERE user_id=? "
                         "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
    return [r["pwd_hash"] for r in rows]


def record_local_failure(user_id: str, failed_count: int,
                         locked_until: Optional[float]) -> None:
    with _conn() as c:
        c.execute("UPDATE local_user SET failed_count=?, locked_until=?, "
                  "status=CASE WHEN ? IS NOT NULL THEN 'locked' ELSE status END "
                  "WHERE user_id=?", (failed_count, locked_until, locked_until, user_id))


def record_local_login(user_id: str, ts: Optional[float] = None) -> None:
    with _conn() as c:
        c.execute("UPDATE local_user SET last_login_at=?, failed_count=0, locked_until=NULL, "
                  "status=CASE WHEN status='locked' THEN 'active' ELSE status END "
                  "WHERE user_id=?", (time.time() if ts is None else ts, user_id))


def count_local_users(status: Optional[str] = None) -> int:
    sql, args = "SELECT COUNT(*) AS n FROM local_user", ()
    if status:
        sql, args = sql + " WHERE status=?", (status,)
    with _conn() as c:
        return int(c.execute(sql, args).fetchone()["n"])


def delete_local_users(keep: Iterable[str] = ()) -> List[str]:
    """物理删除本地账号（**仅维护 / 测试清理用**，日常停用请用 update_local_user_meta）。

    为什么不给"删除账号"当常规功能：账号是审计的归属键，删掉之后
    "这条数是谁问的"就查不到人了。常规下线的正确动作是**停用**。
    `keep` 里的工号不会被删（清理时至少留一个能登录的，否则会把自己锁在门外）。
    """
    keep_set = {str(k).strip() for k in (keep or ()) if str(k).strip()}
    with _conn() as c:
        rows = [r["user_id"] for r in c.execute("SELECT user_id FROM local_user")]
        drop = [u for u in rows if u not in keep_set]
        for u in drop:
            c.execute("DELETE FROM local_user WHERE user_id=?", (u,))
            c.execute("DELETE FROM local_pwd_history WHERE user_id=?", (u,))
            c.execute("DELETE FROM user_session WHERE user_id=?", (u,))
        return drop


def delete_grants_for(user_ids: Iterable[str]) -> int:
    """删掉这些工号的授权台账行（**仅维护 / 测试清理用**）。

    日常撤销授权走 `authz_store.revoke_grant()`（状态置 revoked、保留历史行）；
    这里是真的删除，因为账号本身已经不在了。
    """
    ids = [str(u).strip() for u in (user_ids or ()) if str(u).strip()]
    if not ids:
        return 0
    ph = ",".join("?" for _ in ids)
    with _conn() as c:
        return c.execute(f"DELETE FROM auth_grant WHERE user_id IN ({ph})", ids).rowcount


def table_counts(tables: Iterable[str]) -> Dict[str, int]:
    """这些表的行数（维护页的"清理前有多少条"）。未知表记 0，不抛异常。"""
    out: Dict[str, int] = {}
    with _conn() as c:
        names = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in tables:
            out[t] = (int(c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"])
                      if t in names else 0)
    return out


def purge_tables(tables: Iterable[str]) -> Dict[str, int]:
    """清空这些表并返回清掉的条数（**仅维护 / 测试清理用**）。

    ★ 白名单而不是"传什么删什么"：这个方法会被管理端接口间接调用，
      将来若有人从请求体透传表名，白名单是最后一道闸。
    """
    # `audit_log` 是本库里 v0.20 之前的审计底档（现役审计在 var/audit.db）。
    # 放进白名单是为了"清测试数据"能把这批底档一起清掉；正式环境不要动它。
    allowed = {"snapshot", "feedback", "golden_backlog", "caliber_pref", "source_state",
               "metric_version", "export_log", "chat_session", "chat_message",
               "auth_event", "auth_grant_log", "user_session", "local_pwd_history",
               "audit_log"}
    out: Dict[str, int] = {}
    with _conn() as c:
        names = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in tables:
            if t not in allowed or t not in names:
                continue
            n = int(c.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"])
            if n:
                c.execute(f"DELETE FROM {t}")
            # 自增号一起归零：否则清完之后新建的第一条记录 id 是 730 而不是 1，
            # 演示环境里看起来像"没清干净"。
            try:
                c.execute("DELETE FROM sqlite_sequence WHERE name=?", (t,))
            except Exception:
                pass
            out[t] = n
    return out


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    """运行：python -m app.core.meta_db --selftest

    ★ 审计链的攻防用例（改一条 / 删一条 / 重算整链 / 截断尾部）已移到
      `app/core/audit.py --selftest` —— 审计现在住在独立库里，
      在这里造审计数据会变成"到别人的库里写测试数据"。
      本自测只保证**转发接线**是通的（路由层依赖这三个转发函数）。
    """
    import tempfile
    from pathlib import Path as _Path

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    print("元数据库自测（审计转发 + 授权变更链，docs/12 §6.2 / §4.4）")
    old = (settings.meta_db_path, settings.audit_path, settings.audit_key_path,
           settings.audit_anchor_path, settings.audit_hmac_key)
    tmpdir = _Path(tempfile.gettempdir()) / f"ask_meta_selftest_{int(time.time() * 1000)}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    settings.meta_db_path = str(tmpdir / "meta.db")     # 自测绝不碰真实库
    settings.audit_path = str(tmpdir / "audit.db")
    settings.audit_key_path = str(tmpdir / ".key")
    settings.audit_anchor_path = str(tmpdir / "anchor.jsonl")
    settings.audit_hmac_key = ""
    try:
        # ① 审计转发：写进独立库、从独立库读回
        for i in range(3):
            log_audit("u005", "信息科", f"问题{i}", "answered", "query_metric",
                      "ip_discharge_cnt", {"i": i}, "SELECT 1", 1, "h", 10, f"ans{i}")
        v = verify_chain()
        check("审计转发到独立库且链成立", v["ok"] and v["checked"] == 3,
              f"checked={v.get('checked')} reason={v.get('reason', '')}")
        check("审计库与业务元数据库**不是同一个文件**",
              settings.audit_path != settings.meta_db_path,
              _Path(settings.audit_path).name)
        rows = list_audit(limit=10, user_id="u005")
        check("审计查询转发正确（按人过滤）", len(rows) == 3, str(len(rows)))
        st = audit_stats()
        check("审计现状可查（路径/纳链条数/密钥来源/锚数量）",
              st["chained"] == 3 and "key_id" in st["key_source"], st["key_source"])

        # ② 授权变更链：HMAC + 改造前旧算法行的兼容
        log_authz_change("u005", "u002", "grant", role="科室主任", dept_scope='["0101"]',
                         source="manual", source_ref="WF-1", reason="测试")
        log_authz_change("u005", "u002", "revoke", source="manual", source_ref="WF-2")
        av = verify_authz_chain()
        check("授权变更链用 HMAC 保护", av["ok"] and av["legacy_algo"] == 0,
              str(av.get("note"))[:56])
        # 把第二条换成"裸 sha256"（模拟改造前写入的行）→ 应被识别为 legacy_algo 而非"被改"
        with _conn() as c:
            r = dict(c.execute("SELECT * FROM auth_grant_log WHERE id=2").fetchone())
            parts = ["" if r.get(k) is None else str(r.get(k)) for k in _AUTHZ_HASH_FIELDS]
            old_h = hashlib.sha256(("|".join(parts) + "|" + (r["prev_hash"] or "")
                                    ).encode("utf-8")).hexdigest()
            c.execute("UPDATE auth_grant_log SET hash=? WHERE id=2", (old_h,))
        av2 = verify_authz_chain()
        check("改造前用裸 sha256 写的行被标为 legacy_algo（不误报成「被篡改」）",
              av2["ok"] and av2["legacy_algo"] == 1 and av2["checked"] == 2,
              str(av2.get("note"))[:56])
        # 真改了内容：必须失败
        with _conn() as c:
            c.execute("UPDATE auth_grant_log SET reason='偷偷改的理由' WHERE id=1")
        av3 = verify_authz_chain()
        check("授权变更被真改 → 链失败并定位", (not av3["ok"]) and av3["broken_at"] == 1,
              f"broken_at={av3.get('broken_at')}")
    finally:
        (settings.meta_db_path, settings.audit_path, settings.audit_key_path,
         settings.audit_anchor_path, settings.audit_hmac_key) = old
        for f in tmpdir.rglob("*"):
            try:
                f.unlink()
            except OSError:
                pass
        try:
            tmpdir.rmdir()
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
