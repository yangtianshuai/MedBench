# -*- coding: utf-8 -*-
"""测试数据一键清理（运维 / 交付前 / 演示环境用，**不是业务功能**）。

## 为什么要有这个模块

系统跑一段时间后，元数据库里会堆满"试出来的数据"：体检问句的审计、对话会话、
反馈、回流问题、导出记录、登录事件…… 交付给院方之前需要回到干净状态；
而这批数据分散在 `var/askdata.db`、`var/audit.db` 与锚文件（`audit.anchor.jsonl`）里，
手工删容易漏、也容易把配置一起删掉。

## 三条边界（写死在代码里，不靠界面自觉）

1. **只清"发生过什么"，不清"配了什么"**：账号、授权台账可单独选；语义层、物理映射、
   数据源注册、口径偏好之外的配置一律不碰。
2. **审计清理是复位，不是删几条**：审计库是 HMAC 哈希链 + 库外链锚，删中间几条会
   直接断链。这里做的是**整体复位**（清空记录 + 清空锚 + 立刻封一个空锚），
   所以只有测试/演示环境能用；正式环境请走归档，不要用本模块。
3. **不删调用者自己的账号与登录态**：清理完还得能继续用，否则等于"一键把自己锁在门外"。

## 分组

| code | 名称 | 内容 |
|---|---|---|
| `qa` | 问答记录与审计 | `audit.db` 全量 + 老库审计底档 + 快照 / 反馈 / 回流 / 导出追溯 / 口径偏好 |
| `chat` | 对话会话 | `chat_session` / `chat_message` |
| `identity` | 登录事件 | `auth_event`（在线会话保留：删了会让所有人立刻掉线） |
| `accounts` | 本地账号与授权 | `local_user` / 口令历史 / `auth_grant` / 授权变更留痕（**保留调用者**） |

自测：`python -m app.core.maintenance --selftest`
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.core import meta_db

# 清理动作的二次确认短语（与"切换数据源"同一套做法：防的不是攻击者，是手滑）
CLEAR_CONFIRM = "我确认清理测试数据"

GROUPS: List[Dict[str, Any]] = [
    {"code": "qa", "name": "问答记录与审计",
     "desc": "审计库（含链锚）、老库审计底档、口径快照、反馈、回流问题、导出追溯、口径偏好",
     "tables": ["snapshot", "feedback", "golden_backlog", "export_log", "caliber_pref"],
     "audit": True},
    {"code": "chat", "name": "对话会话",
     "desc": "会话列表与消息回放（不含审计：谁问过什么仍在审计里）",
     "tables": ["chat_session", "chat_message"]},
    {"code": "identity", "name": "登录事件",
     "desc": "登录 / 登出 / 票据失败等身份事件留痕；在线会话保留，避免把所有人踢下线",
     "tables": ["auth_event"]},
    {"code": "accounts", "name": "本地账号与授权",
     "desc": "本地账号、口令历史、授权台账、授权变更留痕（**调用者自己的账号保留**）",
     "accounts": True},
]

_CODES = [g["code"] for g in GROUPS]

# 账号组涉及的元数据表（只读统计 + 清理范围说明）
_ACCOUNT_TABLES = ["local_user", "local_pwd_history", "auth_grant", "auth_grant_log"]
_AUDIT_LEGACY_TABLES = ["audit_log"]


def _audit_group() -> Dict[str, Any]:
    return next(g for g in GROUPS if g.get("audit"))


def inventory() -> Dict[str, Any]:
    """清理前现状：每组多少条（界面据此展示"将清掉 727 条"）。"""
    from app.core import audit

    out: List[Dict[str, Any]] = []
    for g in GROUPS:
        if g.get("accounts"):
            counts = meta_db.table_counts(_ACCOUNT_TABLES)
            out.append({"code": g["code"], "name": g["name"], "desc": g["desc"],
                        "counts": counts, "total": sum(counts.values())})
            continue
        counts = meta_db.table_counts(g.get("tables") or [])
        extra: Dict[str, Any] = {}
        if g.get("audit"):
            try:
                st = audit.stats()
                counts["audit.db"] = int(st.get("total") or 0)
            except Exception as e:                      # 审计库不可读时如实写 0 并说明
                counts["audit.db"] = 0
                extra["audit_error"] = str(e)
            counts.update(meta_db.table_counts(_AUDIT_LEGACY_TABLES))
            out.append({"code": g["code"], "name": g["name"], "desc": g["desc"],
                        "counts": counts, "total": sum(counts.values()), **extra})
            continue
        out.append({"code": g["code"], "name": g["name"], "desc": g["desc"],
                    "counts": counts, "total": sum(counts.values())})
    return {
        "groups": out,
        "local_users": meta_db.count_local_users(),
        "audit_path": settings.audit_path,
        "audit_anchor_path": settings.audit_anchor_path,
        "meta_db_path": settings.meta_db_path,
        "confirm_phrase": CLEAR_CONFIRM,
        "note": "清理不可撤销；账号组会保留调用者自己的账号，其余组不影响登录",
    }


# ────────────────────────────────────────────────────────────
# 审计复位（整体复位，不是删几条）
# ────────────────────────────────────────────────────────────
def _reset_audit() -> Dict[str, Any]:
    """清空审计库 + 链锚，并封一个空锚。

    ★ 为什么要清链锚：只清库不清锚，下一次校验就会报"锚里有 N 条、库里只剩 0 条"
      —— 那正是"删除尾部记录"的判定特征。清完立刻封一个 count=0 的新锚，
      校验才是自洽的。
    ★ 为什么连老库 `audit_log` 一起清：那是 v0.20 迁移前的底档，
      留着它会让"清完还剩 727 条"永远成立（且看起来像没清干净）。
    """
    from app.core import audit

    before = 0
    try:
        before = int(audit.stats().get("total") or 0)
    except Exception:
        before = 0

    # ① 老库底档（先清：否则 audit.migrate_from_meta 会在下次启动时又搬回来）
    legacy = meta_db.purge_tables(_AUDIT_LEGACY_TABLES)

    # ② 审计库：必须先把"只允许追加"的触发器摘掉，否则 DELETE 会被库层拒绝
    p = Path(settings.audit_path)
    if p.exists():
        con = sqlite3.connect(str(p))
        try:
            con.execute("DROP TRIGGER IF EXISTS audit_log_no_update")
            con.execute("DROP TRIGGER IF EXISTS audit_log_no_delete")
            con.execute("DELETE FROM audit_log")
            con.execute("DELETE FROM audit_meta")
            try:
                con.execute("DELETE FROM sqlite_sequence WHERE name='audit_log'")
            except sqlite3.Error:
                pass
            con.commit()
        finally:
            con.close()

    # ③ 链锚：清空后立刻封一个空锚（见上面"为什么要清链锚"）
    ap = Path(settings.audit_anchor_path)
    if ap.exists():
        ap.write_text("", encoding="utf-8")
    try:
        audit.seal(reason="test_cleanup")
    except OSError:
        pass

    out = {"audit_rows": before, "audit_legacy": legacy}
    if settings.audit_mirror_path and Path(settings.audit_mirror_path).exists():
        out["mirror_note"] = (f"镜像文件 {settings.audit_mirror_path} 未清理（它可能在共享盘上，"
                              "且被日志平台采集）：校验时会报「镜像比库多」，属预期")
    return out


# ────────────────────────────────────────────────────────────
# 执行清理
# ────────────────────────────────────────────────────────────
def clear(scopes: List[str], *, actor: str,
          keep_user: Optional[str] = None,
          ip: Optional[str] = None) -> Dict[str, Any]:
    """按分组清理。返回每组清掉的条数（`actor` 只用于留痕与"保留自己账号"）。"""
    wanted = [s for s in (scopes or []) if s in _CODES]
    unknown = [s for s in (scopes or []) if s not in _CODES]
    if not wanted:
        return {"ok": False, "error": f"没有可清理的分组（可选：{', '.join(_CODES)}）"}

    cleared: Dict[str, Any] = {}

    # ① 账号组先做：它会给"作者自己"留一条会话，后清身份事件时不影响
    if "accounts" in wanted:
        from app.core import local_accounts
        keep = [keep_user] if keep_user else []
        res = local_accounts.purge_users(keep=keep, actor=actor,
                                         reason="测试数据一键清理")
        cleared["accounts"] = {
            "local_user": res["deleted_count"],
            "auth_grant": res["deleted_grants"],
            "auth_grant_log": meta_db.purge_tables(["auth_grant_log"]).get("auth_grant_log", 0),
            "kept": res["kept"],
        }

    # ② 对话会话
    if "chat" in wanted:
        cleared["chat"] = meta_db.purge_tables(["chat_message", "chat_session"])

    # ③ 问答记录与审计（审计是整体复位）
    if "qa" in wanted:
        tables = list(_audit_group().get("tables") or [])
        got = meta_db.purge_tables(tables)
        got.update(_reset_audit())
        cleared["qa"] = got

    # ④ 登录事件
    if "identity" in wanted:
        cleared["identity"] = meta_db.purge_tables(["auth_event"])

    # ★ 清理本身也要留痕：否则"数据什么时候没的"没人说得清。
    #   放在最后写：即使 identity 组也被清，这一条仍然在。
    total = sum(v for grp in cleared.values() if isinstance(grp, dict)
                for k, v in grp.items()
                if isinstance(v, int) and not isinstance(v, bool) and k != "kept")
    meta_db.log_auth_event(
        "test_data_cleared", actor, settings.auth_mode, "maintenance",
        f"{actor} 清理测试数据：分组={','.join(wanted)}；合计 {total} 条", ip)
    return {"ok": True, "cleared": cleared, "scopes": wanted, "unknown": unknown,
            "total": int(total),
            "note": "清理不可撤销；账号组保留了调用者自己的账号，审计已复位并封了空锚"}


# ────────────────────────────────────────────────────────────
# 自测：`python -m app.core.maintenance --selftest`
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    import tempfile

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"[{'OK' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))

    print("测试数据清理自测（账号复位 + 审计复位）")
    tmpdir = Path(tempfile.gettempdir()) / f"ask_maint_{int(time.time() * 1000)}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    old = (settings.meta_db_path, settings.audit_path, settings.audit_key_path,
           settings.audit_anchor_path, settings.audit_anchor_every, settings.audit_hmac_key,
           settings.auth_local, settings.auth_mode)
    settings.meta_db_path = str(tmpdir / "meta.db")
    settings.audit_path = str(tmpdir / "audit.db")
    settings.audit_key_path = str(tmpdir / ".key")
    settings.audit_anchor_path = str(tmpdir / "anchor.jsonl")
    settings.audit_anchor_every = 0
    settings.audit_hmac_key = ""
    settings.auth_local = "primary"
    settings.auth_mode = "sso"
    try:
        from app.core import audit, local_accounts

        meta_db.init_db()

        # 造数据：两个账号 + 授权 + 会话 + 审计 + 快照 + 对话
        r1 = local_accounts.create_user("adm1", "管理员甲", actor="cli", reason="自测",
                                        valid_days=30, enabled=True)
        local_accounts.create_user("doc1", "医生乙", actor="cli", reason="自测",
                                   valid_days=30, enabled=True)
        from app.core import authz_store
        authz_store.upsert_grant("adm1", "信息科", dept_scope=["*"], campus_scope=["*"],
                                 source="manual", source_ref="自测", actor="cli",
                                 reason="自测")
        sid = meta_db.create_session("doc1", "自测会话")
        meta_db.append_message(sid, "user", "doc1", question="自测问题")
        meta_db.save_snapshot({"answer_id": "a1", "ts": time.time(), "user_id": "doc1",
                               "question": "自测", "metric_code": "m", "metric_ver": "1",
                               "caliber_json": "{}", "dsl_json": "{}", "sql_text": "select 1",
                               "result_json": "{}", "result_hash": "h", "data_as_of": "",
                               "maturity": ""})
        audit.log("doc1", "医务处", "自测问题", "answered", "query_metric", "m", {}, "select 1",
                  1, "h", 10, "a1")
        audit.log("doc1", "医务处", "自测问题2", "answered", "query_metric", "m", {}, "select 1",
                  1, "h", 10, "a2")

        inv = inventory()
        qa = next(g for g in inv["groups"] if g["code"] == "qa")
        acc = next(g for g in inv["groups"] if g["code"] == "accounts")
        check("统计口径可用（qa 含审计 2 条 + 快照 1 条）",
              qa["counts"].get("audit.db") == 2 and qa["counts"].get("snapshot") == 1,
              str(qa["counts"]))
        check("账号组统计（2 个账号 + 1 条授权）",
              acc["counts"].get("local_user") == 2 and acc["counts"].get("auth_grant") == 1,
              str(acc["counts"]))

        res = clear(["qa", "chat", "identity", "accounts"], actor="adm1", keep_user="adm1")
        check("清理成功且分组齐全",
              res["ok"] and set(res["scopes"]) == {"qa", "chat", "identity", "accounts"})
        check("审计被清空（0 条）", audit.stats()["total"] == 0,
              str(audit.stats()["total"]))
        check("审计链复位后仍然自洽（空锚已封，不误报「尾部被删」）",
              audit.verify_chain()["ok"] is True,
              str(audit.verify_chain().get("reason", ""))[:40])
        check("快照/会话已清空",
              meta_db.table_counts(["snapshot", "chat_session", "chat_message"])
              == {"snapshot": 0, "chat_session": 0, "chat_message": 0})
        check("调用者自己的账号保留（否则等于一键把自己锁在门外）",
              meta_db.count_local_users() == 1
              and bool(meta_db.get_local_user("adm1"))
              and meta_db.get_local_user("doc1") is None)
        check("被删账号的授权台账一并清掉，保留者的授权还在",
              meta_db.table_counts(["auth_grant"])["auth_grant"] == 1,
              str(meta_db.table_counts(["auth_grant"])))
        check("清理动作本身留痕", any(
            e["kind"] == "test_data_cleared"
            for e in meta_db.list_auth_events(limit=10)))

        # 清空账号后，初始化入口重新打开
        local_accounts.purge_users(keep=[], actor="cli")
        check("账号清空后回到「需要初始化」状态",
              local_accounts.bootstrap_state()["needs_bootstrap"] is True)
        boot = local_accounts.create_first_admin("adm2", "新管理员", "Hospital@2026abc",
                                                 "Hospital@2026abc", ip="127.0.0.1")
        check("首次初始化建管理员（并写入授权台账）",
              boot["ok"] and boot["role"] == "信息科",
              str(boot.get("error", "")))
        check("再调一次初始化入口被关闭（不允许创建第二个管理员）",
              local_accounts.create_first_admin("adm3", "第三人", "Hospital@2026abc",
                                                "Hospital@2026abc")["ok"] is False)
        check("初始化账号可直接登录（口令本人设置，不强制改密）",
              local_accounts.login("adm2", "Hospital@2026abc")["ok"] is True)
    finally:
        (settings.meta_db_path, settings.audit_path, settings.audit_key_path,
         settings.audit_anchor_path, settings.audit_anchor_every, settings.audit_hmac_key,
         settings.auth_local, settings.auth_mode) = old
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
