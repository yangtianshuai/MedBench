# -*- coding: utf-8 -*-
"""组织架构 → 授权台账的同步逻辑（F6.2 · docs/12 §4.2 / §4.3）。

这里只有**逻辑**（读源 / 映射 / 出计划 / 落库）：
命令行入口在 `scripts/sync_authz.py`，管理端入口在 `/api/security/grants/sync`。
两者共用同一份实现 —— 否则"界面上预览的计划"和"命令行执行的计划"会慢慢跑偏。

四条不可放宽的原则（每条都对应一种真实事故）：
  1. **映射不到就整体不导**（fail-closed）：一半人导进去的状态，没人说得清算不算完成；
     更糟的是没人知道还差谁。
  2. **撤销必须显式打开**（`revoke_missing`）：一份只覆盖部分科室的导出，
     会把名单外所有人的权限撤光 —— 这是这套机制里最危险的一次误操作。
  3. **只增不改**：变更走"撤旧 + 写新 + 留痕"，台账里永远查得到历史。
  4. **依据必填**（ref）：没有依据的授权，半年后没人回答得出"这个权限是谁给的"。
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.config import settings
from app.core import authz_store, meta_db, permission


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    p = Path(path or settings.authz_sync_path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ────────────────────────────────────────────────────────────
# 读源
# ────────────────────────────────────────────────────────────
def source_yaml() -> List[Dict[str, Any]]:
    """roles.yaml 的 users 段 → 待同步条目（**迁移用**）。

    ★ 关键：把 YAML 的隐式语义**显式化**。YAML 里"没写 dept_scope"会回落到
      角色的 `default_dept_scope`；搬进台账时必须把回落**后**的值写死 ——
      台账里的空值是"无授权"（见 authz_store 约束 C）。
      不这么做，迁移当天就会把一批人从"能看全院"变成"什么都看不到"，
      而且系统不会有任何报错（范围为空在预检阶段表现为 denied）。
    """
    cfg = permission.load_roles()
    out = []
    for uid, u in (cfg.get("users") or {}).items():
        # ★ 必须走 resolve_from_yaml：直接 resolve_user 会尊重 ASK_AUTHZ_FROM，
        #   而"先切严格模式、再迁移"这种顺序下读到的全是"无授权" ——
        #   迁移会把全院导成无权限，且不报任何错。
        eff = permission.resolve_from_yaml(uid)
        out.append({"user_id": uid, "name": (u or {}).get("name", uid),
                    "role": eff["role"], "dept_scope": eff["dept_scope"],
                    "campus_scope": eff["campus_scope"], "raw_dept": ""})
    return out


def source_file(path: str, fields: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """院方组织架构导出（CSV / JSON）→ 待同步条目（**科室仍是名字，待映射**）。"""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"找不到组织架构文件：{p}")
    f = {"user_id": "工号", "name": "姓名", "dept": "科室", "post": "岗位", "campus": "院区"}
    f.update({k: v for k, v in (fields or {}).items() if v})
    if p.suffix.lower() in (".json", ".jsonl"):
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data if isinstance(data, list) else (data or {}).get("rows", [])
    else:
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        out.append({"user_id": str(r.get(f["user_id"], "")).strip(),
                    "name": str(r.get(f["name"], "")).strip(),
                    "role": str(r.get(f["post"], "")).strip(),       # 岗位名，待映射
                    "raw_dept": str(r.get(f["dept"], "")).strip(),    # 科室名，待映射
                    "raw_campus": str(r.get(f["campus"], "")).strip(),
                    "dept_scope": None, "campus_scope": None})
    return [x for x in out if x["user_id"]]


# ────────────────────────────────────────────────────────────
# 映射（不猜）
# ────────────────────────────────────────────────────────────
def map_rows(rows: List[Dict[str, Any]], cfg: Dict[str, Any],
             source: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """→ (可写入的行, 映射失败的行)。失败的行会带 `why`（直接展示给运维看）。"""
    role_map = cfg.get("role_map") or {}
    dept_map = cfg.get("dept_map") or {}
    alias = {str(k): str(v) for k, v in (cfg.get("dept_alias") or {}).items()}
    defaults = cfg.get("defaults") or {}
    campus_default = defaults.get("campus_scope")
    ok, bad = [], []
    for r in rows:
        if source == "yaml":
            ok.append(r)                      # YAML 已是角色码 + 科室码，无需映射
            continue
        role = role_map.get(r.get("role"))
        if not role:
            bad.append({**r, "why": f"岗位「{r.get('role')}」没有映射到角色码"
                                   f"（改 semantic-layer/authz_sync.yaml 的 role_map）"})
            continue
        raw_dept = r.get("raw_dept")
        code = dept_map.get(raw_dept)
        remapped = False
        # SCD2：科室更名/合并按 alias 迁移，并留痕（导出的名字可能还是旧码对应的新名）
        if code and code in alias:
            code, remapped = alias[code], True
        if not code:
            bad.append({**r, "why": f"科室「{raw_dept}」没有映射到科室码"
                                   f"（台账锚点是科室码，不能用名字；见 authz_sync.yaml dept_map）"})
            continue
        ok.append({**r, "role": role, "dept_scope": [code],
                   "campus_scope": ([r["raw_campus"]] if r.get("raw_campus")
                                    else campus_default),
                   "remapped": remapped, "dept_code": code})
    return ok, bad


# ────────────────────────────────────────────────────────────
# 计划（预览与执行共用，免得"看到的"和"执行的"不是一回事）
# ────────────────────────────────────────────────────────────
def build_plan(rows: List[Dict[str, Any]], source: str, ref: str, valid_days: int,
               revoke_missing: bool = False) -> Tuple[List[Dict[str, Any]], float]:
    plan: List[Dict[str, Any]] = []
    now = time.time()
    for r in rows:
        cur = authz_store.active_grant(r["user_id"])
        want_role = r["role"]
        want_scope = list(r.get("dept_scope") or [])
        want_campus = r.get("campus_scope")
        if cur is None:
            plan.append({"action": "grant", "user": r["user_id"], "name": r.get("name"),
                         "role": want_role, "dept_scope": want_scope,
                         "campus_scope": want_campus, "why": "台账中无有效授权"})
            continue
        g = authz_store.grant_to_scope(cur)
        if (g["role"] == want_role and sorted(g["dept_scope"]) == sorted(want_scope)
                and g["campus_scope"] == want_campus):
            plan.append({"action": "keep", "user": r["user_id"], "name": r.get("name"),
                         "role": want_role, "dept_scope": want_scope,
                         "campus_scope": want_campus, "why": "与台账一致"})
        else:
            plan.append({"action": "update", "user": r["user_id"], "name": r.get("name"),
                         "role": want_role, "dept_scope": want_scope,
                         "campus_scope": want_campus,
                         "why": f"{g['role']}/{g['dept_scope']} → {want_role}/{want_scope}",
                         "remap": bool(r.get("remapped"))})
    if revoke_missing:
        seen = {r["user_id"] for r in rows}
        for row in meta_db.list_grant_rows(status="active"):
            if row["user_id"] not in seen:
                plan.append({"action": "revoke", "user": row["user_id"], "name": "",
                             "role": row.get("role"),
                             "dept_scope": authz_store.loads_scope(row.get("dept_scope")),
                             "campus_scope": authz_store.loads_campus(row.get("campus_scope")),
                             "why": "组织架构快照中已不存在（离职/转岗）"})
    return plan, now


def apply_plan(plan: List[Dict[str, Any]], source: str, ref: str,
               valid_days: int) -> Dict[str, int]:
    actor = f"sync:{source}"
    n = {"grant": 0, "update": 0, "revoke": 0, "failed": 0}
    until = time.time() + valid_days * 86400 if valid_days else None
    for item in plan:
        if item["action"] in ("grant", "update"):
            res = authz_store.upsert_grant(
                item["user"], item["role"], item["dept_scope"],
                campus_scope=item.get("campus_scope"), valid_to=until,
                source=source, source_ref=ref, actor=actor, reason=item["why"])
            if res.get("ok"):
                n[item["action"]] += 1
            else:
                n["failed"] += 1
        elif item["action"] == "revoke":
            if authz_store.revoke_grant(item["user"], actor=actor,
                                        reason=item["why"], source_ref=ref).get("ok"):
                n["revoke"] += 1
            else:
                n["failed"] += 1
    return n


def run(source: str, ref: str, file: Optional[str] = None, apply: bool = False,
        revoke_missing: bool = False, valid_days: Optional[int] = None,
        config_path: Optional[str] = None) -> Dict[str, Any]:
    """给 CLI 与 API 共用的入口。返回计划/结果（不做任何打印）。"""
    cfg = load_config(config_path)
    defaults = cfg.get("defaults") or {}
    days = int(valid_days if valid_days is not None else defaults.get("valid_days", 365))
    if not (ref or "").strip():
        return {"ok": False, "error": "--ref 必填：没有依据的授权，事后无法回答「这个权限是谁给的」"}
    rows = source_yaml() if source == "yaml" else source_file(file or "", cfg.get("fields"))
    good, bad = map_rows(rows, cfg, source)
    if bad:
        return {"ok": False, "error": "存在无法映射的条目，本次未写入任何变更",
                "bad": bad, "read": len(rows), "mapped": len(good), "applied": False,
                "hint": "补 semantic-layer/authz_sync.yaml 的 role_map / dept_map 后重试"}
    plan, _ = build_plan(good, source, ref, days, revoke_missing)
    counts: Dict[str, int] = {}
    for p in plan:
        counts[p["action"]] = counts.get(p["action"], 0) + 1
    out: Dict[str, Any] = {"ok": True, "source": source, "ref": ref, "valid_days": days,
                           "read": len(rows), "mapped": len(good), "plan": plan,
                           "counts": counts, "applied": False}
    if apply:
        out["result"] = apply_plan(plan, source, ref, days)
        out["applied"] = True
        out["chain"] = meta_db.verify_authz_chain()
        out["stats"] = authz_store.stats()
    return out
