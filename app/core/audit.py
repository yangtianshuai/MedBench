# -*- coding: utf-8 -*-
"""审计独立存储（F6.5 · docs/12 §6.2）—— 等保测评会问「你怎么证明日志没被改过」。

## 先说清楚「不可篡改」到底能做到什么程度

把审计从业务库挪到另一个库文件，**本身并不解决问题**：能改一个库的 DBA
两个库都能改。真正起作用的只有三件事，本模块三件都做：

| 防线 | 做法 | 防住什么 | 防不住什么 |
|---|---|---|---|
| ① HMAC 链 | 链哈希用 **HMAC-SHA256**，密钥在**库外**（环境变量或独立密钥文件） | 能改库但**没有密钥**的人：重算不出正确的链 | 同时拿到密钥与库权限的人 |
| ② 链锚 | 每 N 条把 `(条数, 尾哈希, 密钥指纹)` 追加到**另一个位置** | 整链重算、**截断尾部**（删掉最后几条） | 锚文件也被一起改的人（故建议锚指到另一块盘/另一台机器） |
| ③ 库级只允许追加 | SQLite 触发器对 `UPDATE` / `DELETE` 直接 `RAISE(ABORT)` | "顺手改一行"这类绝大多数情况 | `DROP TRIGGER` 后再改（由 ① 兜住） |

②③ 加起来才能检出"截断"：只做链，删掉**尾部**若干条是**不破坏链**的
（链首到删除点都自洽），这是哈希链最容易被忽视的漏洞。

## 一句必须写进交接文档的运维风险

**密钥丢了 = 历史审计链永久无法校验**（不是"无法读取"，是"无法自证未被动过"）。
所以：`ASK_AUDIT_HMAC_KEY` 应由信息科与库一起纳入备份；自动生成的密钥文件
（`var/.audit_hmac_key`）必须单独备份。锚记录里的 `key_id` 就是用来区分
「记录被改了」和「换了密钥」的 —— 两者都表现为校验失败，但处理方式完全不同。

## 遗留数据

v0.20 之前审计写在 `var/askdata.db` 的 `audit_log` 表。迁移时这些行按 **legacy**
导入（`hash` 为空），`verify_chain()` 会如实统计 `legacy_rows` 并**从第一条纳链
记录开始校验** —— 不假装改造前的记录也被保护了。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings

CHAIN_VERSION = "v1"          # 算法版本；将来换算法时老记录仍可校验

# 进程内串行化写入（uvicorn 默认单进程多线程；跨进程由 UNIQUE 约束 + 重试兜底）
_WRITE_LOCK = threading.Lock()

_SCHEMA = """
-- 审计表：只允许追加（下面的触发器强制）
CREATE TABLE IF NOT EXISTS audit_log (
    seq          INTEGER UNIQUE,       -- 链上的序号（1..N），锚点靠它定位
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
    prev_hash    TEXT,
    hash         TEXT,
    imported_from TEXT,              -- 非空 = 从老库搬来的遗留行（未纳入新链）
    -- ★ 遗留段在**老算法**（裸 sha256）下的两条链信息，必须一起搬过来：
    --   只搬 hash 不搬 prev_hash，旧段就永远验不过（缺一头），
    --   于是每次校验都报"遗留记录被改过"—— 一个纯属自己造成的假警报。
    legacy_prev_hash TEXT,
    legacy_hash  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);

-- ★ 只允许追加：任何 UPDATE / DELETE 直接失败。
--   为什么放在库层而不是只放在应用层：应用层只能保证"我们的代码不改"，
--   挡不住有人直接开 sqlite3 命令行改一行。触发器会把这一步变成显式报错。
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, '审计表只允许追加：UPDATE 被拒绝（docs/12 §6.2）'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, '审计表只允许追加：DELETE 被拒绝（docs/12 §6.2）'); END;

CREATE TABLE IF NOT EXISTS audit_meta (k TEXT PRIMARY KEY, v TEXT);
"""

# 哈希字段顺序即**计算顺序**。改这里等于让历史链全部失效，新增字段只能追加到末尾。
_HASH_FIELDS = ("ts", "user_id", "role", "question", "status", "intent",
                "metric_code", "dsl_json", "sql_text", "row_count",
                "result_hash", "latency_ms", "answer_id")


# ────────────────────────────────────────────────────────────
# 库连接
# ────────────────────────────────────────────────────────────
def _conn() -> sqlite3.Connection:
    p = Path(settings.audit_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(p))
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _kv_get(c: sqlite3.Connection, k: str) -> Optional[str]:
    r = c.execute("SELECT v FROM audit_meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else None


def _kv_put(c: sqlite3.Connection, k: str, v: str) -> None:
    c.execute("INSERT INTO audit_meta (k,v) VALUES (?,?) "
              "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))


# ────────────────────────────────────────────────────────────
# 密钥（库外）
# ────────────────────────────────────────────────────────────
def _load_key() -> Tuple[bytes, str, str]:
    """返回 (密钥, 来源说明, 密钥指纹)。环境变量优先，其次密钥文件（自动生成）。"""
    env = (settings.audit_hmac_key or "").strip()
    if env:
        kb = env.encode("utf-8")
        return kb, "env:ASK_AUDIT_HMAC_KEY", key_id(kb)
    kp = Path(settings.audit_key_path)
    if not kp.exists():
        kb = os.urandom(32)
        kp.parent.mkdir(parents=True, exist_ok=True)
        kp.write_bytes(kb)
        try:
            os.chmod(kp, 0o600)          # Windows 上不生效，见 key_source 说明
        except OSError:
            pass
    kb = kp.read_bytes()
    return kb, f"file:{kp}", key_id(kb)


def key_id(kb: bytes) -> str:
    """密钥指纹（前 12 位）。写进锚记录，用来区分「记录被改」与「换了密钥」。"""
    return hashlib.sha256(kb).hexdigest()[:12]


def key_source() -> str:
    _, src, kid = _load_key()
    return f"{src} key_id={kid}"


# ────────────────────────────────────────────────────────────
# 链
# ────────────────────────────────────────────────────────────
def chain_hash_parts(domain: str, parts: List[str], prev_hash: str, key: bytes) -> str:
    """链式哈希的公共实现（HMAC-SHA256 + **域分隔**）。

    `domain` 是域分隔符：审计链用 `v1`、授权变更链用 `authz-v1`。
    没有它的话，两条链的同一份内容会算出同一个哈希 ——
    理论上可以把一条链的记录搬到另一条链里去凑（都是"自洽"的）。
    """
    msg = "|".join(parts) + "|" + (prev_hash or "")
    return hmac.new(key, (domain + "|" + msg).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def chain_hash(row: Dict[str, Any], prev_hash: str, key: bytes) -> str:
    """一条审计记录的链式哈希：内容 + 前一条的哈希，用 **HMAC** 而不是裸 sha256。

    为什么换成 HMAC：裸 sha256 的算法是公开的，能改库的人**可以自己把整条链重算一遍**，
    改完依旧自洽 —— 这正是"哈希链"在真实攻防里最常被高估的地方。
    HMAC 把密钥移出数据库后，重算整链需要同时拿到密钥。
    """
    parts = ["" if row.get(k) is None else str(row.get(k)) for k in _HASH_FIELDS]
    return chain_hash_parts(CHAIN_VERSION, parts, prev_hash, key)


def _tail(c: sqlite3.Connection) -> Tuple[int, str]:
    """链尾：(seq, hash)。只看纳链记录（遗留行 hash 为空，不参与）。"""
    r = c.execute("SELECT seq, hash FROM audit_log WHERE hash IS NOT NULL "
                  "ORDER BY seq DESC LIMIT 1").fetchone()
    return (int(r["seq"]), r["hash"]) if r else (0, "")


def _next_seq(c: sqlite3.Connection) -> int:
    """下一个序号：对**所有**行取 MAX(seq)+1，不只是纳链的行。

    ★ 为什么不能只看链尾：迁移进来的遗留行也占着序号。若遗留行的 seq 比链尾大，
      按链尾 +1 会与遗留行**撞号**（UNIQUE 约束直接报错，一条审计都写不进去）。
    """
    m = c.execute("SELECT COALESCE(MAX(seq),0) m FROM audit_log").fetchone()["m"]
    return int(m) + 1


# ────────────────────────────────────────────────────────────
# 写入 / 查询（签名与 meta_db 保持兼容，路由层无需改动）
# ────────────────────────────────────────────────────────────
def log(user_id: str, role: str, question: str, status: str,
        intent: Optional[str], metric_code: Optional[str],
        dsl: Optional[dict], sql: Optional[str], row_count: int,
        result_hash: Optional[str], latency_ms: int,
        answer_id: Optional[str], ts: Optional[float] = None) -> int:
    """写一条审计。返回记录 id。

    ★ 这条函数**不应该让业务请求失败**：审计写不进去时抛异常，等于用审计把问数搞挂，
      运维的第一反应会是关掉审计。所以失败只上抛给调用方由它记日志（见 engine_bridge），
      但**绝不静默吞掉**（吞掉等于审计有个洞而没人知道）。
    """
    row: Dict[str, Any] = {"ts": ts if ts is not None else time.time(),
                           "user_id": user_id, "role": role, "question": question,
                           "status": status, "intent": intent, "metric_code": metric_code,
                           "dsl_json": json.dumps(dsl, ensure_ascii=False) if dsl else None,
                           "sql_text": sql, "row_count": row_count,
                           "result_hash": result_hash, "latency_ms": latency_ms,
                           "answer_id": answer_id}
    key, _src, _kid = _load_key()
    # ★ 并发：读链尾与插入必须串起来，否则两个同时到达的请求会取到同一个 seq
    #   （表现是 `UNIQUE constraint failed: audit_log.seq`，而且**整个问数接口 500** ——
    #    审计写不进去就把业务打挂了，这是最不该发生的耦合）。
    #   进程内用锁串行，跨进程靠 UNIQUE 约束 + 重试兜底。
    with _WRITE_LOCK:
        rid = _append(row, key)
    _mirror(row)
    if settings.audit_anchor_every > 0 and row["seq"] % settings.audit_anchor_every == 0:
        try:
            seal(reason="auto")
        except OSError:
            pass                      # 锚写不进去不该让问数失败；verify 会如实报出来
    return rid


def _append(row: Dict[str, Any], key: bytes, attempts: int = 5) -> int:
    """写一行，撞号就重试（跨进程并发的兜底）。"""
    last: Optional[Exception] = None
    for i in range(attempts):
        try:
            with _conn() as c:
                _seq, prev = _tail(c)
                row["seq"] = _next_seq(c)
                row["prev_hash"] = prev
                row["hash"] = chain_hash(row, prev, key)
                cur = c.execute(
                    "INSERT INTO audit_log (seq,ts,user_id,role,question,status,intent,"
                    "metric_code,dsl_json,sql_text,row_count,result_hash,latency_ms,"
                    "answer_id,prev_hash,hash) "
                    "VALUES (:seq,:ts,:user_id,:role,:question,:status,:intent,:metric_code,"
                    ":dsl_json,:sql_text,:row_count,:result_hash,:latency_ms,:answer_id,"
                    ":prev_hash,:hash)", row)
                return cur.lastrowid
        except sqlite3.IntegrityError as e:      # 号被别的进程抢了
            last = e
            time.sleep(0.01 * (i + 1))
    raise last if last else RuntimeError("审计写入失败")


def _mirror(row: Dict[str, Any]) -> None:
    """可选的镜像落盘（每行一条 JSONL，便于被日志平台 / WORM 采集）。

    ★ 镜像失败**不抛异常**：镜像通常指向共享盘，盘掉线不该让全院问不了数。
      但不能当没发生 —— `verify_chain()` 会对比镜像行数与库内条数，报出"镜像落后 N 条"。
    """
    p = settings.audit_mirror_path
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def list_(limit: int = 100, user_id: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM audit_log"
    args: List[Any] = []
    if user_id:
        sql += " WHERE user_id = ?"
        args.append(user_id)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    with _conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


# ────────────────────────────────────────────────────────────
# 链锚（回执）
# ────────────────────────────────────────────────────────────
def seal(reason: str = "manual") -> Dict[str, Any]:
    """把当前链尾封一个锚，追加到锚文件（不在审计库里）。

    锚的价值在于"另一份记录"：整链重算或截断尾部之后，
    库里一切自洽，但与早先落下的锚对不上。
    """
    key, src, kid = _load_key()
    with _conn() as c:
        seq, tail = _tail(c)
        count = c.execute("SELECT COUNT(*) n FROM audit_log WHERE hash IS NOT NULL").fetchone()["n"]
    rec = {"ts": time.time(), "seq": seq, "count": int(count), "tail_hash": tail,
           "key_id": kid, "chain_version": CHAIN_VERSION, "reason": reason,
           "key_source": src}
    p = Path(settings.audit_anchor_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def anchors() -> List[Dict[str, Any]]:
    p = Path(settings.audit_anchor_path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"ts": 0, "seq": 0, "count": 0, "tail_hash": "",
                            "reason": "锚文件有一行无法解析（可能被改过）"})
    return out


# ────────────────────────────────────────────────────────────
# 校验
# ────────────────────────────────────────────────────────────
def verify_chain(limit: int = 20000) -> Dict[str, Any]:
    """校验审计链 + 链锚 + 镜像，返回**诚实**的结论。"""
    key, src, kid = _load_key()
    with _conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM audit_log ORDER BY id ASC LIMIT ?", (limit,))]
        chains = c.execute("SELECT COUNT(*) n FROM audit_log WHERE hash IS NOT NULL"
                           ).fetchone()["n"]

    # ── 锚比对（先做）：即使链本身已经失败，也要能说清"是记录被改，还是换了密钥" ──
    ans = anchors()
    bad: List[Dict[str, Any]] = []
    for a in ans:
        if a.get("key_id") and a["key_id"] != kid:
            bad.append({**a, "why": f"锚是用**另一个密钥**（key_id={a['key_id']}）封的"})
        elif int(a.get("count") or 0) > int(chains):
            bad.append({**a, "why": f"锚里已有 {a.get('count')} 条纳链记录，"
                                    f"库里只剩 {chains} 条 —— **尾部的记录被删掉了**"})
    anchor_info = {"anchors": len(ans), "anchor_ok": not bad,
                   "anchor_mismatch": bad[-3:] if bad else []}
    key_hint = (None if not bad or not any("另一个密钥" in b["why"] for b in bad) else
                "链锚是用另一个密钥封的 —— 这更像**换过审计密钥**（如密钥文件被重新生成），"
                "而不是记录被改写；两者都会让校验失败，但处理方式不同")

    legacy = checked = 0
    prev = ""
    first_seq: Optional[int] = None
    tail_seq = 0
    for row in rows:
        if not row.get("hash"):
            legacy += 1
            continue                 # 迁移前的老记录：如实计入，不从它们开始断言
        if first_seq is None:
            first_seq = int(row["seq"] or 0)
        if (row.get("prev_hash") or "") != prev:
            return {"ok": False, "checked": checked, "legacy_rows": legacy,
                    "broken_at": row["id"],
                    "reason": "prev_hash 与前一条记录的 hash 不一致（中间有记录被删或改）",
                    "key_id": kid, "key_source": src, "key_hint": key_hint, **anchor_info}
        if chain_hash(row, prev, key) != row["hash"]:
            return {"ok": False, "checked": checked, "legacy_rows": legacy,
                    "broken_at": row["id"],
                    "reason": "记录内容与自身哈希不符（该行被改动，或换了审计密钥）",
                    "key_id": kid, "key_source": src, "key_hint": key_hint, **anchor_info}
        checked += 1
        tail_seq = int(row["seq"] or 0)
        prev = row["hash"]

    out: Dict[str, Any] = {
        "ok": True, "checked": checked, "legacy_rows": legacy,
        "first_seq": first_seq, "tail_seq": tail_seq, "tail_hash": prev,
        "chain_rows": int(chains), "key_id": kid, "key_source": src,
        "chain_version": CHAIN_VERSION, "key_hint": key_hint,
        "legacy_chain": _verify_legacy(rows), **anchor_info,
    }
    # 尾哈希比对（只有链走通时才知道真正的尾部）
    for a in ans:
        if int(a.get("seq") or 0) and a["seq"] == tail_seq and a.get("tail_hash") != prev:
            out["anchor_ok"] = False
            out["anchor_mismatch"] = [*out["anchor_mismatch"],
                                      {**a, "why": "同一序号上的尾哈希与锚不符 —— 整链被重算过"}]
    if not out["anchor_ok"]:
        out["ok"] = False
        out["reason"] = (out["anchor_mismatch"] or [{}])[-1].get("why", "链锚对不上")

    # ── 镜像比对（配了才有意义）──
    if settings.audit_mirror_path and Path(settings.audit_mirror_path).exists():
        try:
            n = sum(1 for ln in open(settings.audit_mirror_path, encoding="utf-8") if ln.strip())
            out["mirror_lines"] = n
            out["mirror_behind"] = max(0, int(chains) - n)
        except OSError:
            out["mirror_lines"] = None
    out["note"] = ("本库可被有库权限的人整体重算或删除，因此结论同时看**链锚**："
                   "锚在审计库之外，是截断与重算的唯一证据。"
                   f"锚文件：{settings.audit_anchor_path}")
    return out


def _verify_legacy(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """校验**迁移进来的那段历史**在旧算法（裸 sha256）下是否仍然自洽。

    为什么要单独说一下：旧算法保护力弱（谁都算得出），但它毕竟是当时的记录。
    迁进来之后如果连旧链都对不上，说明那些行在迁移前后被动过 ——
    这时应该明说，而不是用一句"它们是 legacy、不受保护"把问题盖过去。
    """
    imported = [r for r in rows if r.get("imported_from")]
    if not imported:
        return {"checked": 0, "ok": True, "note": "无遗留记录"}
    checked = 0
    for r in imported:
        old = r.get("legacy_hash")
        if not old:
            continue
        parts = ["" if r.get(k) is None else str(r.get(k)) for k in _HASH_FIELDS]
        # 用**遗留段自己的** prev_hash 重算（不能用新链的 prev_hash，两者不是一条链）
        expect = hashlib.sha256(("|".join(parts) + "|" + (r.get("legacy_prev_hash") or "")
                                 ).encode("utf-8")).hexdigest()
        if expect != old:
            return {"checked": checked, "ok": False, "broken_at": r["id"],
                    "note": "遗留记录在旧算法下也不自洽 —— 这些行在迁移前就被改过"
                            "（若只是缺 legacy_prev_hash，则说明迁移没把旧链信息搬全）"}
        checked += 1
    return {"checked": checked, "ok": True, "legacy_algo": "sha256",
            "note": "遗留段用旧算法（裸 sha256）校验通过；该算法无密钥，保护力弱于新链"}


def stats() -> Dict[str, Any]:
    with _conn() as c:
        tot = c.execute("SELECT COUNT(*) n FROM audit_log").fetchone()["n"]
        chained = c.execute("SELECT COUNT(*) n FROM audit_log WHERE hash IS NOT NULL"
                            ).fetchone()["n"]
        legacy = tot - chained
        seq, tail = _tail(c)
    return {"path": settings.audit_path, "total": int(tot), "chained": int(chained),
            "legacy": int(legacy), "tail_seq": seq, "tail_hash": tail,
            "key_source": key_source(), "anchors": len(anchors()),
            "anchor_path": settings.audit_anchor_path,
            "mirror_path": settings.audit_mirror_path or None,
            "anchor_every": settings.audit_anchor_every}


# ────────────────────────────────────────────────────────────
# 迁移（老库 var/askdata.db 的 audit_log）
# ────────────────────────────────────────────────────────────
def migrate_from_meta(meta_db_path: Optional[str] = None) -> Dict[str, Any]:
    """把老库的 `audit_log` 搬进本库（幂等；按 id 续传）。

    ★ 搬迁的行一律**标记为 legacy、不回填哈希**：
      它们在此之前的整个生命周期里都没有被 HMAC 保护过，
      给它们补一条链等于**声称一段从未存在的保护**。宁可如实计 legacy。
    """
    mp = Path(meta_db_path or settings.meta_db_path)
    if not mp.exists():
        return {"moved": 0, "note": "老元数据库不存在，无需迁移"}
    src = sqlite3.connect(str(mp))
    src.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in src.execute("PRAGMA table_info(audit_log)")}
        if not cols:
            return {"moved": 0, "note": "老库里没有 audit_log 表"}
    except sqlite3.DatabaseError:
        return {"moved": 0, "note": "老库不可读（不是 SQLite 或已损坏）"}
    with _conn() as c:
        done = int(_kv_get(c, "meta_audit_migrated_upto_id") or 0)
        rows = [dict(r) for r in src.execute(
            "SELECT * FROM audit_log WHERE id > ? ORDER BY id ASC", (done,))]
        seq = _next_seq(c) - 1        # 遗留行也要占序号（见 _next_seq）
        moved = 0
        for r in rows:
            seq += 1
            # ★ `row_count` / `latency_ms` **必须逐字保真**（NULL 就是 NULL）：
            #   旧链的哈希是把空值当空串算的，如果这里"顺手"把 NULL 补成 0，
            #   迁进来的历史行在旧算法下就全部对不上 —— 于是升级当天就会看到
            #   "遗留记录在迁移前被改过"这种吓人但完全错误的结论。
            #   （这条是自测逼出来的：`r.get("row_count") or 0` 正好踩中。）
            c.execute(
                "INSERT OR IGNORE INTO audit_log (seq,ts,user_id,role,question,status,intent,"
                "metric_code,dsl_json,sql_text,row_count,result_hash,latency_ms,answer_id,"
                "prev_hash,hash,imported_from,legacy_prev_hash,legacy_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,'meta.audit_log',?,?)",
                (seq, r.get("ts") or time.time(), r.get("user_id") or "", r.get("role") or "",
                 r.get("question") or "", r.get("status") or "", r.get("intent"),
                 r.get("metric_code"), r.get("dsl_json"), r.get("sql_text"),
                 r.get("row_count"), r.get("result_hash"), r.get("latency_ms"),
                 r.get("answer_id"), r.get("prev_hash"), r.get("hash")))
            moved += 1
        if rows:
            _kv_put(c, "meta_audit_migrated_upto_id", str(rows[-1]["id"]))
            src.close()
            return {"moved": moved, "upto_id": rows[-1]["id"],
                    "note": f"已迁移 {moved} 条为 legacy（未纳链）。"
                            f"迁移完成后老库的 audit_log 表可以只读保留作为底档。"}
    src.close()
    return {"moved": 0, "upto_id": done, "note": "没有新记录需要迁移"}


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    """运行：python -m app.core.audit --selftest"""
    import tempfile

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    print("审计独立存储自测（docs/12 §6.2：HMAC 链 + 链锚 + 只允许追加）")
    stamp = f"{int(time.time() * 1000)}"
    tmpdir = Path(tempfile.gettempdir()) / f"ask_audit_{stamp}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    old = (settings.audit_path, settings.audit_key_path, settings.audit_anchor_path,
           settings.audit_mirror_path, settings.audit_anchor_every, settings.audit_hmac_key,
           settings.meta_db_path)
    settings.audit_path = str(tmpdir / "audit.db")
    settings.audit_key_path = str(tmpdir / ".key")
    settings.audit_anchor_path = str(tmpdir / "anchor.jsonl")
    settings.audit_mirror_path = ""
    settings.audit_anchor_every = 5
    settings.audit_hmac_key = ""

    def W(i: int, q: str = "") -> None:
        log("u005", "信息科", q or f"问题{i}", "answered", "query_metric",
            "ip_discharge_cnt", {"i": i}, "SELECT 1", 1, "h", 10, f"ans{i}")

    def _safe_w(i: int, errs: List[str]) -> None:
        try:
            W(i, f"并发{i}")
        except Exception as e:                  # noqa: BLE001 —— 这里就要把异常收下来
            errs.append(f"{type(e).__name__}: {e}")

    def raw(sql: str, args=()) -> None:
        """绕过触发器直接改库 —— 模拟"有库权限的人"，这是校验必须能抓到的对手。"""
        c = sqlite3.connect(settings.audit_path)
        c.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
        c.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
        c.execute(sql, args)
        c.commit()
        c.close()

    try:
        # ① 正常链
        for i in range(4):
            W(i)
        v = verify_chain()
        check("4 条记录链接得上", v["ok"] and v["checked"] == 4, f"checked={v.get('checked')}")

        # ② 只允许追加：库层直接拒绝改/删
        for sql, what in (("UPDATE audit_log SET question='x' WHERE id=2", "UPDATE"),
                          ("DELETE FROM audit_log WHERE id=2", "DELETE")):
            c = sqlite3.connect(settings.audit_path)
            try:
                c.execute(sql)
                c.commit()
                check(f"库层拒绝 {what}（触发器）", False, "竟然改成功了")
            except sqlite3.IntegrityError as e:
                check(f"库层拒绝 {what}（触发器）", "只允许追加" in str(e), str(e)[:60])
            finally:
                c.close()

        # ③ 密钥不在库里：没有密钥的人重算整链也白算
        c = sqlite3.connect(settings.audit_path)
        c.row_factory = sqlite3.Row
        row = dict(c.execute("SELECT * FROM audit_log WHERE id=2").fetchone())
        c.close()
        raw("UPDATE audit_log SET question='被改过的问题' WHERE id=2")
        v3 = verify_chain()
        check("改一条必被发现且定位到 id", (not v3["ok"]) and v3["broken_at"] == 2,
              f"broken_at={v3.get('broken_at')}")
        raw("UPDATE audit_log SET question=? WHERE id=2", (row["question"],))

        # ④ ★ 整链重算：模拟攻击者删掉中间一条、再用裸 sha256 重算后面所有哈希
        raw("DELETE FROM audit_log WHERE seq=3")
        key, _s, _k = _load_key()
        c = sqlite3.connect(settings.audit_path)
        rows = [dict(zip([d[0] for d in c.execute("SELECT * FROM audit_log ORDER BY seq").description], r))
                for r in c.execute("SELECT * FROM audit_log ORDER BY seq")]
        forged_prev = ""
        for r in rows:
            if not r.get("hash"):
                continue
            fake = hashlib.sha256(("|".join(
                ["" if r.get(f) is None else str(r.get(f)) for f in _HASH_FIELDS]
            ) + "|" + forged_prev + "|sha256-forged").encode()).hexdigest()
            c.execute("UPDATE audit_log SET prev_hash=?, hash=? WHERE id=?",
                      (forged_prev, fake, r["id"]))
            forged_prev = fake
        c.commit()
        c.close()
        v4 = verify_chain()
        check("★ 删一条 + 用公开算法重算整链 → 仍被判失败（HMAC 密钥在库外）",
              (not v4["ok"]), f"ok={v4['ok']} reason={str(v4.get('reason'))[:50]}")

        # ⑤ 锚：检出"截断尾部"（只做链是发现不了的）
        settings.audit_path = str(tmpdir / "audit2.db")
        settings.audit_anchor_every = 0
        for i in range(6):
            W(i)
        a = seal("test")
        seal("test")
        v5 = verify_chain()
        check("锚与链一致时通过", v5["ok"] and v5["anchor_ok"] and v5["anchors"] == 2,
              f"anchors={v5.get('anchors')}")
        raw("DELETE FROM audit_log WHERE seq=6")          # 只删尾部：链本身仍自洽！
        v6 = verify_chain()
        check("★ 只删尾部（链仍自洽）→ 被链锚抓到", (not v6["ok"]) and not v6["anchor_ok"],
              str(v6.get("reason"))[:60])

        # ⑥ 换密钥：要能区分"记录被改"与"密钥换了"
        settings.audit_hmac_key = "another-key-entirely"
        v7 = verify_chain()
        check("换密钥后被识别为「另一个密钥封的锚」而非静默通过",
              (not v7["ok"]) and not v7["anchor_ok"], str(v7.get("reason"))[:50])
        settings.audit_hmac_key = ""

        # ⑦ 遗留行如实计数，且不把后续记录带断
        settings.audit_path = str(tmpdir / "audit3.db")
        settings.audit_anchor_path = str(tmpdir / "anchor3.jsonl")
        W(0)
        c = sqlite3.connect(settings.audit_path)
        c.execute("INSERT INTO audit_log (seq,ts,user_id,role,question,status,imported_from) "
                  "VALUES (99,?,?,?,?,?, 'meta.audit_log')",
                  (time.time(), "u001", "院长", "改造前的老记录", "answered"))
        c.commit()
        c.close()
        W(1)
        v8 = verify_chain()
        check("遗留行计入 legacy_rows 且不带断后续链",
              v8["ok"] and v8["checked"] == 2 and v8["legacy_rows"] == 1,
              f"checked={v8.get('checked')} legacy={v8.get('legacy_rows')}")

        # ⑧ 迁移：老库 audit_log → 本库（幂等；且要保留"旧算法仍自洽"这个信息）
        settings.audit_path = str(tmpdir / "audit_mig.db")     # 换一个干净的审计库
        settings.audit_anchor_path = str(tmpdir / "anchor_mig.jsonl")
        meta = tmpdir / "meta.db"
        mc = sqlite3.connect(str(meta))
        mc.executescript(
            "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, user_id TEXT,"
            " role TEXT, question TEXT, status TEXT, intent TEXT, metric_code TEXT,"
            " dsl_json TEXT, sql_text TEXT, row_count INTEGER, result_hash TEXT,"
            " latency_ms INTEGER, answer_id TEXT, prev_hash TEXT, hash TEXT);")
        prev = ""
        for i in range(3):
            # ts 用浮点：列是 REAL，写整数会被存成 1700000000.0，
            # 而哈希里是 str(...) —— 这里要是用整数，测试自己就先算错了。
            row = {"ts": 1700000000.5 + i, "user_id": "u001", "role": "院长",
                   "question": f"老记录{i}", "status": "answered"}
            parts = ["" if row.get(k) is None else str(row.get(k)) for k in _HASH_FIELDS]
            h = hashlib.sha256(("|".join(parts) + "|" + prev).encode("utf-8")).hexdigest()
            mc.execute("INSERT INTO audit_log (ts,user_id,role,question,status,prev_hash,hash) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (row["ts"], row["user_id"], row["role"], row["question"],
                        row["status"], prev, h))
            prev = h
        mc.commit()
        mc.close()
        settings.meta_db_path = str(meta)
        m1 = migrate_from_meta()
        m2 = migrate_from_meta()
        check("迁移老库审计记录（幂等：第二次搬 0 条）",
              m1["moved"] == 3 and m2["moved"] == 0, f"{m1['moved']} / {m2['moved']}")
        check("迁移后如实报告 legacy 行数（不假装它们被保护过）",
              stats()["legacy"] == 3, str(stats()["legacy"]))
        v10 = verify_chain()
        check("遗留段用旧算法单独校验（旧链对不上就要明说，不能用「反正它是遗留」盖过去）",
              v10["legacy_chain"]["ok"] and v10["legacy_chain"]["checked"] == 3,
              str(v10["legacy_chain"])[:60])

        # ⑨ 镜像落后要能报出来（共享盘掉线时不能当没发生）
        settings.audit_path = str(tmpdir / "audit4.db")
        settings.audit_anchor_path = str(tmpdir / "anchor4.jsonl")
        settings.audit_mirror_path = str(tmpdir / "mirror.jsonl")
        W(0)
        W(1)
        Path(settings.audit_mirror_path).write_text("", encoding="utf-8")   # 模拟镜像丢失
        v9 = verify_chain()
        check("镜像落后会被报出来（不静默）", v9.get("mirror_behind") == 2,
              str(v9.get("mirror_behind")))

        # ⑩ 并发写入：审计不能因为"号撞了"而写失败
        #    ★ 这条来自一次真实故障：读链尾与插入之间没有串起来，
        #      两个并发请求取到同一个 seq → UNIQUE 冲突 → **整个 /api/ask 500**。
        #      审计写不进去就把业务打挂，是最不该有的耦合。
        settings.audit_path = str(tmpdir / "audit5.db")
        settings.audit_anchor_path = str(tmpdir / "anchor5.jsonl")
        settings.audit_mirror_path = ""
        import threading as _th
        errs: List[str] = []
        ts_ = [_th.Thread(target=lambda i=i: _safe_w(i, errs)) for i in range(12)]
        for t in ts_:
            t.start()
        for t in ts_:
            t.join()
        v11 = verify_chain()
        check("12 个并发写入全部成功且不撞号（曾导致 /api/ask 500）",
              not errs and v11["ok"] and v11["checked"] == 12,
              f"errs={errs[:2]} checked={v11.get('checked')}")
    finally:
        (settings.audit_path, settings.audit_key_path, settings.audit_anchor_path,
         settings.audit_mirror_path, settings.audit_anchor_every, settings.audit_hmac_key,
         settings.meta_db_path) = old
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
