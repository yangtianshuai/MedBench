# -*- coding: utf-8 -*-
"""授权台账（F6.2 · docs/12 §4.2 / §4.3 / §4.4）—— 「谁能看哪些科室」的唯一事实来源。

为什么必须从 `roles.yaml` 搬到数据库（这不是"换个地方存"）：
    roles.yaml 是**代码配置**：改一次授权 = 改文件 + 走发布 + 重启服务。
    而"某人转岗、某人离职、某科室合并"是每天都会发生的事。
    配置改不动日常，日常就会绕过配置 —— 权限系统最危险的状态不是"配错"，
    是**没人维护**（最后变成所有人都是当初那个人设的、一直没动过）。

因此本产品只做两件事：
    1. `roles.yaml` 保留为**角色定义模板**（角色能做什么：可见指标 / 能否导出 / 能否看 SQL）；
    2. 「谁能担任该角色、覆盖哪些科室、到什么时候」放本台账，由同步任务从
       院方组织架构刷入（`scripts/sync_authz.py`），**不建第二套人事主数据**。

三条硬约束（对应 §4.2 与 §4.4）：
    A. **依据必填**：任何一条授权没有 `source_ref`（组织架构版本 / 工单号 / 备案号）
       就是"不知道谁批的"，拒绝写入 —— 这是唯一能回答"这个权限谁给的"的字段。
    B. **同一人同一时刻只有一条有效授权**：写新的先撤旧的（同一事务 + 留痕）。
       两条并存时"谁生效"取决于查询顺序，等于权限随机。
    C. **表里的空 ≠ yaml 里的空**：`dept_scope` 缺失/坏值一律按 `[]`（无授权）处理，
       **不回落**角色的 `default_dept_scope`。理由：yaml 的"未配置"是人工写漏，
       而台账的"未配置"是同步任务漏填 —— 前者有角色默认值兜底的习惯，
       后者一旦兜底就等于把"漏填"变成"全院"。

自测：`python -m app.core.authz_store --selftest`
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from app.config import settings
from app.core import meta_db

# 授权来源（写进台账与留痕，决定"出问题时找谁"）
SOURCES = ("hrp", "his", "yaml", "manual")
ACTIONS = ("grant", "update", "revoke", "remap", "expire")


# ────────────────────────────────────────────────────────────
# 基础：范围序列化（JSON 数组；坏值一律按"无授权"）
# ────────────────────────────────────────────────────────────
def dumps_scope(scope: Optional[List[str]]) -> str:
    """科室范围 → JSON 字符串。None 存成 `[]`（= 无授权），**不存 NULL**。"""
    return json.dumps(list(scope or []), ensure_ascii=False)


def loads_scope(raw: Any) -> List[str]:
    """读科室范围。★ 解析失败一律返回 `[]`（无授权）而不是 `["*"]`。

    这是本模块最重要的一行：JSON 坏掉、列被改空、迁移写错，
    任何"读不出来"的情形都必须落到**最保守**的那一侧。
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(v, list):
        return []
    return [str(x) for x in v]


def loads_campus(raw: Any) -> Optional[List[str]]:
    """读院区范围。与科室不同：**NULL 就是 NULL**（= 未启用院区围栏，不是全院）。

    语义必须与 roles.yaml 严格一致，否则同一个账号在两套来源下行为不同
    （表现为"从 yaml 迁到台账后，某主任突然查不到数据了"）。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return [str(x) for x in v] if isinstance(v, list) else None


# ────────────────────────────────────────────────────────────
# 生效期
# ────────────────────────────────────────────────────────────
def in_effect(row: Dict[str, Any], at: Optional[float] = None) -> bool:
    """是否在生效期内。半开区间 `[valid_from, valid_to)`。"""
    import time
    now = time.time() if at is None else at
    vf, vt = row.get("valid_from"), row.get("valid_to")
    if vf is not None and now < vf:
        return False
    if vt is not None and now >= vt:
        return False
    return True


def active_grant(user_id: str, at: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """取某工号当前有效的授权行（没有则 None = 这个人没有台账授权）。"""
    best: Optional[Dict[str, Any]] = None
    for row in meta_db.list_grant_rows(user_id=user_id):
        if row.get("status") != "active":
            continue
        if not in_effect(row, at):
            continue
        # 约束 B 的兜底：理论上不会出现两条，出现了也要**确定地**取一条
        # （按 valid_from 取最新），而不是让调用方拿到随机的那个。
        if best is None or (row.get("valid_from") or 0) >= (best.get("valid_from") or 0):
            best = row
    return best


def grant_to_scope(row: Dict[str, Any]) -> Dict[str, Any]:
    """台账行 → 权限判定用的字段（与 permission.resolve_user 的输出同形）。"""
    return {"user_id": row.get("user_id"), "role": row.get("role"),
            "dept_scope": loads_scope(row.get("dept_scope")),
            "campus_scope": loads_campus(row.get("campus_scope")),
            "source": row.get("source"), "source_ref": row.get("source_ref"),
            "valid_from": row.get("valid_from"), "valid_to": row.get("valid_to"),
            "grant_id": row.get("grant_id")}


# ────────────────────────────────────────────────────────────
# 写入（唯一入口：改台账必须留痕）
# ────────────────────────────────────────────────────────────
def _role_exists(role: str) -> bool:
    """角色必须在 roles.yaml 的 roles 段里 —— 否则写进去也解析不出任何权限。

    ★ 为什么在这里拦：写进一个不存在的角色码，`resolve_user` 会得到一个空角色
      （无 visible_metrics、无 default_scope）。用户以为"授权成功了"，
      实际是"什么都没授"或"按 guest 处理"，而且**不报错**。
    """
    from app.core import permission
    return role in (permission.load_roles().get("roles") or {})


def upsert_grant(user_id: str, role: str, dept_scope: Optional[List[str]] = None,
                 campus_scope: Optional[List[str]] = None,
                 valid_from: Optional[float] = None, valid_to: Optional[float] = None,
                 source: str = "manual", source_ref: str = "",
                 actor: str = "system", reason: str = "",
                 allow_role_create: bool = False) -> Dict[str, Any]:
    """新增/变更一条授权。**不原地改内容**：撤销旧行 + 写新行 + 两条留痕。

    返回 `{ok, grant_id, action, before, after}`；校验失败返回 `{ok: False, error}`。
    """
    if not user_id:
        return {"ok": False, "error": "工号为空"}
    if not role:
        return {"ok": False, "error": "角色为空"}
    if not allow_role_create and not _role_exists(role):
        from app.core import permission
        known = ", ".join(sorted((permission.load_roles().get("roles") or {}).keys()))
        return {"ok": False, "error": f"角色 {role!r} 未在 roles.yaml 中定义（可用：{known}）"}
    # 约束 A：依据必填
    if not (source_ref or "").strip():
        return {"ok": False, "error": "缺少授权依据 source_ref（组织架构版本号 / 工单号 / 备案号）；"
                                      "没有依据的授权无法回答「这个权限是谁给的」"}
    if source not in SOURCES:
        return {"ok": False, "error": f"未知来源 {source!r}（可用：{', '.join(SOURCES)}）"}
    if valid_from is not None and valid_to is not None and valid_to <= valid_from:
        return {"ok": False, "error": "失效时间早于生效时间"}
    if dept_scope is None:
        # 不允许"不填"：调用方必须明确说 `[]`（无授权）还是 `["*"]`（全院）
        return {"ok": False, "error": "dept_scope 必须显式给出：[]=无授权、['*']=全院"}

    old = active_grant(user_id)
    before = grant_to_scope(old) if old else None
    # 撤销旧行（约束 B）
    for row in meta_db.list_grant_rows(user_id=user_id, status="active"):
        if old and row["grant_id"] == old["grant_id"]:
            continue
        meta_db.set_grant_status(row["grant_id"], "revoked")
    if old:
        meta_db.set_grant_status(old["grant_id"], "revoked")

    import time
    now = time.time()
    gid = "g-" + uuid.uuid4().hex[:16]
    row = {"grant_id": gid, "user_id": user_id, "role": role,
           "dept_scope": dumps_scope(dept_scope),
           "campus_scope": (json.dumps(list(campus_scope), ensure_ascii=False)
                            if campus_scope is not None else None),
           "valid_from": valid_from, "valid_to": valid_to,
           "source": source, "source_ref": source_ref, "status": "active",
           "synced_at": now, "created_at": now, "updated_at": now}
    meta_db.put_grant(row)
    if old:
        meta_db.log_authz_change(actor, user_id, "update", role=role,
                                 dept_scope=row["dept_scope"],
                                 campus_scope=row["campus_scope"],
                                 valid_from=valid_from, valid_to=valid_to,
                                 source=source, source_ref=source_ref, reason=reason,
                                 detail=_diff_text(before, grant_to_scope(row)))
    else:
        meta_db.log_authz_change(actor, user_id, "grant", role=role,
                                 dept_scope=row["dept_scope"],
                                 campus_scope=row["campus_scope"],
                                 valid_from=valid_from, valid_to=valid_to,
                                 source=source, source_ref=source_ref, reason=reason,
                                 detail=f"新增：{role} / {_scope_text(dept_scope)}")
    return {"ok": True, "grant_id": gid, "action": "update" if old else "grant",
            "before": before, "after": grant_to_scope(row)}


def revoke_grant(user_id: str, actor: str = "system", reason: str = "",
                 source_ref: str = "") -> Dict[str, Any]:
    """撤销某人当前授权（离职、转岗、审计要求）。

    ★ 撤销**不删行**：删掉之后"他曾经有过什么权限"就查不到了，
      而这恰恰是审计时唯一需要的答案。
    """
    old = active_grant(user_id)
    if not old:
        return {"ok": False, "error": f"{user_id} 当前没有有效授权"}
    meta_db.set_grant_status(old["grant_id"], "revoked")
    meta_db.log_authz_change(actor, user_id, "revoke", role=old.get("role"),
                             dept_scope=old.get("dept_scope"),
                             campus_scope=old.get("campus_scope"),
                             source=old.get("source"), source_ref=source_ref or old.get("source_ref"),
                             reason=reason or "撤销授权",
                             detail=f"撤销：{old.get('role')} / "
                                    f"{_scope_text(loads_scope(old.get('dept_scope')))}")
    return {"ok": True, "revoked": old["grant_id"], "before": grant_to_scope(old)}


def expire_due(actor: str = "system") -> List[Dict[str, Any]]:
    """把已过失效期的授权显式标成 expired（临时授权的"自动回收"）。

    ★ 为什么要有这个动作：`active_grant()` 已经不认过期行（判定即失效），
      所以这不是安全需要，而是**管理需要** —— 台账里一直躺着 active 的过期行，
      管理端就没法回答"现在到底有多少人还有权限"。
    """
    import time
    out = []
    now = time.time()
    for row in meta_db.list_grant_rows(status="active"):
        vt = row.get("valid_to")
        if vt is not None and now >= vt:
            meta_db.set_grant_status(row["grant_id"], "expired")
            meta_db.log_authz_change(actor, row.get("user_id"), "expire",
                                     role=row.get("role"),
                                     dept_scope=row.get("dept_scope"),
                                     campus_scope=row.get("campus_scope"),
                                     valid_from=row.get("valid_from"), valid_to=vt,
                                     source=row.get("source"), source_ref=row.get("source_ref"),
                                     reason="到期自动失效", detail="到期回收")
            out.append(row)
    return out


# ────────────────────────────────────────────────────────────
# 查询（管理端「授权台账」页）
# ────────────────────────────────────────────────────────────
def list_grants(include_inactive: bool = False) -> List[Dict[str, Any]]:
    rows = meta_db.list_grant_rows(status=None if include_inactive else "active")
    out = []
    for r in rows:
        g = grant_to_scope(r)
        g.update({"status": r.get("status"), "synced_at": r.get("synced_at"),
                  "updated_at": r.get("updated_at"), "in_effect": in_effect(r)})
        out.append(g)
    return out


def stats() -> Dict[str, Any]:
    """台账现状：管理端要能一眼看出"还有多少人是靠 yaml 兜着的"。"""
    import time
    from app.core import permission
    active = meta_db.list_grant_rows(status="active")
    yaml_users = list((permission.load_roles().get("users") or {}).keys())
    table_users = {r["user_id"] for r in active}
    warn = settings.authz_expiry_warn_days * 86400
    now = time.time()
    expiring = [r["user_id"] for r in active
                if r.get("valid_to") is not None and 0 <= (r["valid_to"] - now) <= warn]
    pending = [r["user_id"] for r in active if r.get("valid_to") is None]
    return {"authz_from": settings.authz_from,
            "active_grants": len(active),
            "table_users": sorted(table_users),
            "yaml_only_users": sorted(set(yaml_users) - table_users),
            "expiring_soon": sorted(expiring),
            "no_expiry": sorted(pending),
            "log_total": len(meta_db.list_authz_log(limit=100000)),
            "sources": settings.authz_from and sorted({r.get("source") or "?" for r in active})}


def _scope_text(scope: Optional[List[str]]) -> str:
    if not scope:
        return "无授权"
    if "*" in scope:
        return "全院"
    return "、".join(scope)


def _diff_text(before: Optional[Dict[str, Any]], after: Dict[str, Any]) -> str:
    if not before:
        return "—"
    bits = []
    for k, name in (("role", "角色"), ("dept_scope", "科室范围"), ("campus_scope", "院区范围")):
        if before.get(k) != after.get(k):
            bits.append(f"{name}：{_fmt(before.get(k))} → {_fmt(after.get(k))}")
    return "；".join(bits) or "内容未变（仅续期）"


def _fmt(v: Any) -> str:
    if isinstance(v, list):
        return _scope_text(v)
    return "未启用" if v is None else str(v)


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    """运行：python -m app.core.authz_store --selftest"""
    import tempfile
    import time
    from pathlib import Path

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    print("授权台账自测（docs/12 §4.2 / §4.3 / §4.4）")
    old = settings.meta_db_path
    tmp = Path(tempfile.gettempdir()) / f"ask_authz_store_{int(time.time() * 1000)}.db"
    settings.meta_db_path = str(tmp)
    try:
        # ① 依据必填：没有依据的授权无法回答"这个权限谁给的"
        r = upsert_grant("E001", "科室主任", ["0101"], source_ref="", actor="t")
        check("缺依据（source_ref）→ 拒绝写入", not r["ok"], str(r.get("error"))[:60])
        r = upsert_grant("E001", "不存在的角色", ["0101"], source_ref="WF-1")
        check("角色未在 roles.yaml 定义 → 拒绝（否则「授权成功」其实是空权限）",
              not r["ok"], str(r.get("error"))[:70])
        r = upsert_grant("E001", "科室主任", None, source_ref="WF-1")
        check("dept_scope 不明说 → 拒绝（必须明确 [] 还是 ['*']）", not r["ok"], str(r.get("error"))[:50])

        # ② 正常写入 + 留痕
        r = upsert_grant("E001", "科室主任", ["0101", "0102"], source="hrp",
                         source_ref="HRP-2026-10", actor="sync:hrp", reason="组织架构同步")
        check("首次授权 → 落库并留痕", r["ok"] and r["action"] == "grant", r.get("grant_id", ""))
        logs = meta_db.list_authz_log(grantee="E001")
        check("留痕里 actor 是同步任务（与「谁被授了」分开记）",
              logs and logs[0]["actor"] == "sync:hrp" and logs[0]["grantee"] == "E001")
        check("留痕带依据 source_ref（HRP-2026-10）", logs[0]["source_ref"] == "HRP-2026-10")

        # ③ 约束 B：同一人同一时刻只有一条 active，改授权 = 撤旧 + 写新
        r2 = upsert_grant("E001", "科室主任", ["0101"], source="hrp",
                          source_ref="HRP-2026-11", actor="sync:hrp", reason="科室合并")
        actives = [x for x in meta_db.list_grant_rows(user_id="E001") if x["status"] == "active"]
        check("改授权后只有一条 active（两条并存 = 谁生效看查询顺序）",
              len(actives) == 1 and actives[0]["grant_id"] == r2["grant_id"],
              f"{len(actives)} 条")
        check("旧行被标记 revoked 而不是删除（审计要能查到他曾经有什么）",
              any(x["status"] == "revoked" and x["grant_id"] == r["grant_id"]
                  for x in meta_db.list_grant_rows(user_id="E001")))
        up = [x for x in meta_db.list_authz_log(grantee="E001") if x["action"] == "update"]
        check("变更留痕写了 before → after", up and "→" in (up[0]["detail"] or ""),
              (up[0]["detail"] if up else "")[:60])
        check("生效范围以台账为准（0101，旧授权 0102 已失效）",
              grant_to_scope(active_grant("E001"))["dept_scope"] == ["0101"])

        # ④ 生效期：半开区间，过期即失效
        now = time.time()
        upsert_grant("E002", "科室主任", ["0201"], source="manual", source_ref="WF-9",
                     valid_from=now - 100, valid_to=now - 1, actor="t")
        check("已过期的授权 → active_grant 取不到（判定即失效，不依赖定时任务）",
              active_grant("E002") is None)
        upsert_grant("E003", "科室主任", ["0301"], source="manual", source_ref="WF-9",
                     valid_from=now + 3600, actor="t")
        check("未到生效时间的授权 → 取不到（提前录入不提前生效）", active_grant("E003") is None)
        upsert_grant("E004", "科室主任", ["0401"], source="manual", source_ref="WF-9",
                     valid_from=now - 10, valid_to=now + 10, actor="t")
        check("生效期内 → 取得到", active_grant("E004") is not None)
        check("生效期是半开区间：at=valid_to 那一刻已经失效",
              active_grant("E004", at=now + 10) is None and active_grant("E004", at=now + 9) is not None)
        n = expire_due()
        check("到期回收把过期行标成 expired（管理端才数得清「现在谁还有权限」）",
              len(n) == 1 and n[0]["user_id"] == "E002", f"回收 {len(n)} 条")

        # ⑤ 约束 C：坏数据一律落到"无授权"这一侧
        check("dept_scope 是坏 JSON → 按 []（无授权）而不是按全院",
              loads_scope("{坏数据") == [] and loads_scope(None) == [])
        check("dept_scope 是个字符串 → 按 [] 处理（不把 '0101' 当列表）",
              loads_scope('"0101"') == [])
        check("campus_scope 缺失 → None（未启用院区围栏），不是全院、也不是无授权",
              loads_campus(None) is None and loads_campus("") is None)
        check("campus_scope 显式 [] → 无授权（与 dept_scope 语义一致）",
              loads_campus("[]") == [])

        # ⑥ 撤销：不删行
        rv = revoke_grant("E001", actor="u005", reason="离职")
        rows = meta_db.list_grant_rows(user_id="E001")
        check("撤销后没有 active 行，但历史行都在",
              rv["ok"] and active_grant("E001") is None and len(rows) >= 2, f"{len(rows)} 行")
        check("撤销也留痕（action=revoke，带原因与操作者）",
              any(x["action"] == "revoke" and x["reason"] == "离职" and x["actor"] == "u005"
                  for x in meta_db.list_authz_log(grantee="E001")))
        check("重复撤销 → 明确报错（不静默成功）", not revoke_grant("E001")["ok"])

        # ⑦ 哈希链：改了留痕要能发现
        chain = meta_db.verify_authz_chain()
        check("授权变更链校验通过", chain["ok"], f"checked={chain['checked']}")
        check("链上每条都有哈希（新库不该有 legacy 行）", chain["legacy_rows"] == 0)

        # ⑧ 台账现状统计（管理端「还有多少人靠 yaml 兜着」）
        st = stats()
        check("统计能回答「台账覆盖率」：列出只在 yaml 里的用户",
              isinstance(st["yaml_only_users"], list) and "E001" not in st["table_users"])
    finally:
        settings.meta_db_path = old
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
