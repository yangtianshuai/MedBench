# -*- coding: utf-8 -*-
"""角色与数据范围（F6.2 / F6.3）。

设计要点：
  - 权限判定**早于**反问：越权请求若先被反问，会变成 need_clarify 而漏放，
    且反问本身泄露了"这份数据存在"（04 §1 已固化为硬约束）
  - 行级权限在 SQL 层注入（engine/sql_builder 已实现 dept_code IN (...)），
    下钻不得绕过
  - 列级：按 sensitivity 三级语义判定（v0.10 与执行层对齐）：
      public = 所有人可见；internal = 院内有效角色可见；
      restricted = 需在语义层 permission.restricted_metrics_roles 内。
    ★ 授权名单以语义层为唯一事实来源，不另建一份（见 docs/12 §1.2 S9）
  - ★ 数据范围语义（v0.9 修正）：
      ["*"] = 明确的全院授权；[] = 无任何范围（必须拒绝）；None = 由角色默认值推导。
    调用方一律禁止 `dept_scope or ["*"]` 兜底 —— 那会把「无授权」变成「全院」
    （曾导致未授权访客可查全院 internal 指标，详见 docs/12 §1.2 S2）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.config import settings

_cache: Optional[Dict[str, Any]] = None


def load_roles(force: bool = False) -> Dict[str, Any]:
    global _cache
    if _cache is None or force:
        p = Path(settings.roles_path)
        if not p.exists():
            _cache = {"roles": {}, "users": {}}
        else:
            with p.open("r", encoding="utf-8") as f:
                _cache = yaml.safe_load(f) or {"roles": {}, "users": {}}
    return _cache


def list_roles() -> List[Dict[str, Any]]:
    cfg = load_roles()
    return [{"code": k, **(v or {})} for k, v in (cfg.get("roles") or {}).items()]


def resolve_from_yaml(user_id: str, at: Optional[float] = None) -> Dict[str, Any]:
    """**强制按 YAML 名单**解析（忽略 `ASK_AUTHZ_FROM`）。

    ★ 为什么必须单独给一个入口：授权迁移（`authz_sync.source_yaml`）要读的是
      "YAML 里这个人本该是什么范围"。如果它走 `resolve_user()`，而当时
      `ASK_AUTHZ_FROM=table`（很自然的一种顺序：先切严格模式、再迁移），
      读到的就是"台账里没有 → 无授权"，于是**迁移把全院导成无权限** --
      而且界面上一切正常、没有任何报错。自测里固化这条（见 §4.2 用例）。
    """
    return resolve_user(user_id, at, mode="yaml")


def resolve_user(user_id: str, at: Optional[float] = None,
                 mode: Optional[str] = None) -> Dict[str, Any]:
    """解析用户 → 角色 + 数据范围。未知用户按最保守策略（guest）。

    **两个来源，一个组装路径**（docs/12 §4.2）：
      - `auth_grant` 台账（数据库）：谁担任什么角色、覆盖哪些科室、到什么时候 —— 日常维护在这；
      - `roles.yaml`：角色定义模板（角色能做什么）+ 迁移期的用户名单。
    `ASK_AUTHZ_FROM` 决定要不要回落：`auto`（迁移期双读）/ `table`（上线后只认台账）/ `yaml`。

    ★ 未知用户必须与 roles.yaml 里的 guest 角色走**同一条**组装路径。
      两个分支各写一遍就会出现「两个 guest 语义不同」——
      未知用户分支曾漏掉 visible_metrics，使未授权用户反而看得到指标目录
      （v0.10 修正）。三个来源（台账 / YAML / 未知）共用下面这一条出口。
    """
    # ★ 这里**不做缓存**：台账查询是本地 SQLite 的一次小查询，而缓存会带来
    #   "撤了权还生效"的窗口期 —— 权限系统里最不该省的就是这一次查询。
    #   （对应验收 H17：撤销后同一秒再问就该被拒。）
    cfg = load_roles()
    mode = (mode or settings.authz_from or "auto").strip().lower()
    grant = None
    if mode in ("auto", "table"):
        from app.core import authz_store
        grant = authz_store.active_grant(user_id, at)

    yaml_user = ((cfg.get("users") or {}).get(user_id)) or {}
    if grant is not None:
        # 台账命中：**范围只认台账**（不取 YAML 的 dept_scope），角色能力取模板。
        # ★ 必须经 grant_to_scope() 解析：台账里存的是 JSON 字符串，
        #   直接 list() 会得到 ['[', '"', '0', ...] 这种"字符串拆成字符"的范围 ——
        #   它非空、看起来有值、SQL 里 IN ('[') 永远查不到数（自测里当场抓到）。
        g = authz_store.grant_to_scope(grant)
        src, u = "table", {"role": g["role"], "dept_scope": g["dept_scope"],
                           "campus_scope": g["campus_scope"],
                           "name": yaml_user.get("name", user_id)}
    elif yaml_user and mode != "table":
        src, u = "yaml", yaml_user
    else:
        # 要么确实查无此人，要么是 `table` 严格模式下的"台账里没有"
        src, u = ("table_miss" if mode == "table" else "unknown"), {}

    role_code = u.get("role") or "guest"
    role = (cfg.get("roles") or {}).get(role_code) or {}
    scope = u.get("dept_scope")
    if scope is None and src == "yaml":
        # ★ 只有 YAML 来源才允许回落到角色的 default_dept_scope：
        #   台账里"没写"是同步任务漏填，兜底成角色默认（常常是全院）等于把漏填变成越权。
        scope = role.get("default_dept_scope", [])

    # ★ 院区范围（F6.3）。语义与 dept_scope 只在"未配置"这一档不同：
    #     未配置（角色与用户都没写 campus_scope）→ None = **不启用院区围栏**；
    #     显式 []  = 无授权 → 拒绝；["*"] = 全院区。
    #   为什么不照 dept_scope 那样把"未配置"也当无授权：
    #   那是**一次性收紧所有既有账号**，升级当天全院问数会被全拒 —— 属于重大行为变更。
    #   院区码是院方口径，必须由 roles.yaml 显式声明后才生效（docs/12 §3.5）。
    campus = u.get("campus_scope")
    if campus is None and src == "yaml":
        campus = role.get("default_campus_scope")

    out = {"user_id": user_id, "role": role_code, "dept_scope": list(scope or []),
           "campus_scope": (list(campus) if campus is not None else None),
           "name": u.get("name", user_id),
           "visible_metrics": role.get("visible_metrics"),
           "can_export": bool(role.get("can_export", True)),
           "can_view_sql": bool(role.get("can_view_sql", False)),
           # admin 只影响「能看别人的快照/审计」，不扩大任何数据范围（12 §3.4）
           "is_admin": bool(role.get("admin", False)),
           # 界面上要能回答"我的权限是哪来的、依据是什么、什么时候到期"
           "auth_source": src}
    if grant is not None:
        out.update({"grant_id": g["grant_id"], "grant_source": g["source"],
                    "grant_source_ref": g["source_ref"], "valid_to": g["valid_to"]})
    if src == "unknown":
        out["warning"] = "未知用户，按最小权限处理"
    elif src == "table_miss":
        out["warning"] = "授权台账中无有效授权（ASK_AUTHZ_FROM=table，不回落 YAML 名单）"
    return out


def admin_roles() -> List[str]:
    """`roles.yaml` 里带 `admin: true` 的角色码（按文件顺序）。

    为什么要有这个函数：首次初始化要"建一个管理员"，而"管理员"是配置里的概念
    （`roles.yaml` 的 `admin` 标记），不能在代码里写死成某个角色名 ——
    写死之后院方改角色配置，初始化出来的账号就不是管理员了。
    """
    return [r["code"] for r in list_roles() if r.get("admin")]


def default_admin_role(fallback: str = "信息科") -> str:
    """初始化账号用的管理员角色：取 `roles.yaml` 里第一个 `admin: true`。"""
    codes = admin_roles()
    return codes[0] if codes else fallback


def is_admin(user: Dict[str, Any]) -> bool:
    """运维/审计角色：可见他人快照与审计（12 §6.3）。

    ★ 这是一条**独立的**授权，不与数据范围混用：
      admin 不代表能看更多科室的数据，只代表能看"谁在什么时候问过什么"。
    """
    return bool(user.get("is_admin"))


def list_demo_accounts() -> List[Dict[str, Any]]:
    """演示身份清单（仅 demo 模式使用）。

    ★ 这不是「客户端自选身份」：账号清单由**服务端**给出，
      gateway/sso 模式下不会返回任何账号，前端也没有切换器（docs/12 §2 红线 4）。
    ★ 清单里的范围必须走 `resolve_user()` 取**生效值**：直接读 YAML 的话，
      台账里改过的账号会在下拉框里显示旧范围 —— 用户按界面上显示的范围去问，
      实际被另一个范围拦下来，是最难解释的一类投诉。
    """
    cfg = load_roles()
    roles = cfg.get("roles") or {}
    out: List[Dict[str, Any]] = []
    for uid in (cfg.get("users") or {}):
        u = resolve_user(uid)
        out.append({
            "user_id": uid,
            "name": u.get("name", uid),
            "role": u.get("role") or "guest",
            "role_name": (roles.get(u.get("role")) or {}).get("name", u.get("role")),
            "dept_scope": u.get("dept_scope") or [],
            # None = 未启用院区围栏（管理端要如实显示，不能显示成"全院"）
            "campus_scope": u.get("campus_scope"),
            "auth_source": u.get("auth_source"),
        })
    return out


def _restricted_metric_roles() -> Optional[List[str]]:
    """受限指标的授权角色 —— 以语义层为唯一事实来源。

    ★ 为什么不去读 roles.yaml 的 sensitive_metrics：
      执行层（engine/sql_builder.py:359）读的是语义层的
      permission.restricted_metrics_roles。目录层若读另一份配置，两层必然跑偏
      —— 表现为「能问出数、驾驶舱却 0 个指标」（docs/12 §1.2 S9）。
      单一事实来源还带来一个好处：改授权只需动语义层一处，执行层与目录层同时生效。
    """
    try:
        from app.core.registry import load_registry
        roles = ((load_registry().get("permission") or {})
                 .get("restricted_metrics_roles"))
        return list(roles) if isinstance(roles, list) else None
    except Exception:
        return None


def can_see_metric(user: Dict[str, Any], metric: Dict[str, Any]) -> bool:
    """列级权限：按 sensitivity 三级语义判定（与执行层对齐）。

      public     —— 所有人可见
      internal   —— 院内有效角色可见，不需额外授权
      restricted —— 需角色在语义层 permission.restricted_metrics_roles 内

    回退：语义层未声明 restricted_metrics_roles 时，才用 roles.yaml 的
    sensitive_metrics 做显式授权（向后兼容）。
    """
    sens = metric.get("sensitivity", "public")
    if sens in ("public", "internal"):
        return True
    roles = _restricted_metric_roles()
    if roles is not None:
        return user.get("role") in roles
    allowed = (load_roles().get("roles") or {}).get(user.get("role"), {}).get("sensitive_metrics")
    return allowed == "*" or (isinstance(allowed, list) and metric.get("code") in allowed)


def filter_metrics(user: Dict[str, Any], metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    visible = user.get("visible_metrics")
    # ★ 显式空列表 = 无授权（v0.9 修正），不是「不限制」。
    #   None / "*" 才表示「未单独限制」；[] 必须返回空集，
    #   否则 guest 的驾驶舱会把全部指标列出来（指标目录本身也是信息泄露）。
    if visible == []:
        return []
    out = []
    for m in metrics:
        if visible and visible != "*" and m.get("code") not in visible:
            continue
        if not can_see_metric(user, m):
            continue
        out.append(m)
    return out


# ────────────────────────────────────────────────────────────
# 自测：列级权限的结论必须与执行层一致
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    """运行：python -m app.core.permission --selftest"""
    from app.core import registry as reg

    metrics = reg.list_metrics()
    registry_permission = (reg.load_registry().get("permission") or {})
    restricted_roles = set(registry_permission.get("restricted_metrics_roles") or [])
    role_codes = list((load_roles().get("roles") or {}).keys())

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    check(f"指标总数 > 0（{len(metrics)} 个）", len(metrics) > 0)
    check(f"语义层已声明受限角色（{len(restricted_roles)} 个）", bool(restricted_roles))

    # 1) 未知用户 = 无授权：目录必须为空（不是「不限制」）
    u = resolve_user("u999")
    check("未知用户：指标目录为空",
          u["role"] == "guest" and not filter_metrics(u, metrics), f"role={u['role']}")

    # 2) 科室主任：internal 指标可见（此前因目录层语义跑偏而为 0）
    u = resolve_user("u002")
    codes = {m["code"] for m in filter_metrics(u, metrics)}
    check("科室主任：internal 指标可见", "ip_discharge_cnt" in codes,
          f"可见 {len(codes)}/{len(metrics)}")

    # 3) ★ 一致性断言：目录层结论 == 执行层规则（全角色 × 全指标）
    #    执行层规则（engine/sql_builder.py:359）：
    #      restricted 指标「且」语义层声明了角色名单时，角色必须在名单内。
    bad = []
    for role_code in role_codes:
        catalog_user = {"role": role_code, "visible_metrics": None}
        for m in metrics:
            catalog = can_see_metric(catalog_user, m)
            execute = (m.get("sensitivity") != "restricted"
                       or not restricted_roles
                       or role_code in restricted_roles)
            if catalog != execute:
                bad.append(f"{role_code}/{m['code']}")
    check(f"目录层与执行层结论一致（{len(role_codes)} 角色 × {len(metrics)} 指标）",
          not bad,
          (f"{len(bad)} 处不一致：" + ", ".join(bad[:5])) if bad else "0 处不一致")

    print("=" * 60)
    print("自测结果: " + ("全部通过" if passed else "存在失败"))
    return passed


# ────────────────────────────────────────────────────────────
# 自测：授权台账（F6.2 · docs/12 §4.2）
# ────────────────────────────────────────────────────────────
def selftest_authz() -> bool:
    """运行：python -m app.core.permission --selftest（本函数并入其中）"""
    import tempfile
    import time
    from pathlib import Path

    from app.core import authz_store

    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    print("授权台账接线自测（docs/12 §4.2）")
    old_db, old_from = settings.meta_db_path, settings.authz_from
    tmp = Path(tempfile.gettempdir()) / f"ask_perm_authz_{int(time.time() * 1000)}.db"
    settings.meta_db_path = str(tmp)
    try:
        # ① 台账优先于 YAML：u002 在 YAML 里是 0101，台账改成 0103
        yaml_scope = resolve_user("u002")["dept_scope"]
        r = authz_store.upsert_grant("u002", "科室主任", ["0103"], source="hrp",
                                     source_ref="HRP-2026-10", actor="sync:hrp")
        u = resolve_user("u002")
        check("台账命中 → 范围以台账为准（不再用 YAML 的 0101）",
              r["ok"] and u["dept_scope"] == ["0103"] and u["auth_source"] == "table",
              f"yaml={yaml_scope} → table={u['dept_scope']}")
        check("台账命中 → 带上依据与到期时间（界面要能回答「谁给的、到什么时候」）",
              u.get("grant_source") == "hrp" and u.get("grant_source_ref") == "HRP-2026-10"
              and "valid_to" in u)
        check("角色能力仍取 YAML 模板（台账只管「谁、哪些科室、到何时」）",
              u["can_export"] is True and u["is_admin"] is False and u["role"] == "科室主任")

        # ② 台账里的空范围**不回落**角色默认值（YAML 来源才回落）
        authz_store.upsert_grant("u003", "医务处", [], source="manual", source_ref="WF-1",
                                 actor="u005")
        u3 = resolve_user("u003")
        check("台账写 [] → 无授权（不回落到医务处的 default_dept_scope=['*']）",
              u3["dept_scope"] == [] and u3["auth_source"] == "table",
              f"{u3['dept_scope']}")

        # ③ 收敛开关：ASK_AUTHZ_FROM=table 时台账里没有就是无授权
        settings.authz_from = "table"
        u4 = resolve_user("u004")
        check("table 严格模式：台账里没有 → 无授权，**不回落 YAML 名单**",
              u4["role"] == "guest" and u4["dept_scope"] == []
              and u4["auth_source"] == "table_miss", f"{u4['auth_source']}/{u4['role']}")
        check("table 严格模式：台账里有 → 正常授权",
              resolve_user("u002")["dept_scope"] == ["0103"])
        settings.authz_from = "yaml"
        check("yaml 模式：台账只作留痕，判定仍走 YAML",
              resolve_user("u002")["auth_source"] == "yaml"
              and resolve_user("u002")["dept_scope"] == yaml_scope)
        settings.authz_from = "auto"

        # ④ 迁移必须在**严格模式**下也能读到 YAML 的真实值
        #    （先切 ASK_AUTHZ_FROM=table 再迁移是很自然的顺序；若迁移走 resolve_user，
        #      读到的就是"台账里没有 → 无授权"，于是迁移把全院导成无权限且不报错）
        settings.authz_from = "table"
        check("严格模式下 resolve_user 读不到 YAML（预期行为）",
              resolve_user("u004")["dept_scope"] == [] and resolve_user("u004")["role"] == "guest")
        check("★ 但 resolve_from_yaml 仍能读到 YAML 的真实范围（迁移专用入口）",
              resolve_from_yaml("u004")["role"] == "财务处"
              and resolve_from_yaml("u004")["dept_scope"] == ["*"],
              f"{resolve_from_yaml('u004')['role']}/{resolve_from_yaml('u004')['dept_scope']}")
        settings.authz_from = "auto"

        # ⑤ 执行层拿到的范围必须是台账那份（否则预检与 RLS 会用另一个范围）
        authz_store.upsert_grant("u002", "科室主任", ["0101"], source="hrp",
                                 source_ref="HRP-2026-11", actor="sync:hrp", reason="科室合并")
        check("台账改回 0101 后，判定立刻跟着变（无缓存残留）",
              resolve_user("u002")["dept_scope"] == ["0101"])
        check("旧授权行被撤销而不是删除（可复查他曾经有过什么）",
              len([x for x in authz_store.list_grants(include_inactive=True)
                   if x["user_id"] == "u002"]) == 2
              and len([x for x in authz_store.list_grants() if x["user_id"] == "u002"]) == 1)

        # ⑤ 台账覆盖率统计（迁移期的核心可观测指标）
        st = authz_store.stats()
        check("统计口径：列出「只在 YAML 里、还没进台账」的用户（迁移进度）",
              "u004" in st["yaml_only_users"] and "u002" in st["table_users"],
              f"yaml_only={st['yaml_only_users'][:3]}")

        # ⑥ 演示账号清单必须显示**生效范围**（否则下拉框显示的范围与实际判定不一致）
        acc = {a["user_id"]: a for a in list_demo_accounts()}
        check("演示身份清单里的范围取自生效值（台账 0101，不是 YAML 的 0101 巧合值）",
              acc["u003"]["dept_scope"] == [] and acc["u003"]["auth_source"] == "table",
              f"{acc['u003']['dept_scope']}")
    finally:
        settings.meta_db_path, settings.authz_from = old_db, old_from
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
        ok = selftest()
        ok = selftest_authz() and ok
        sys.exit(0 if ok else 1)
    print(__doc__)
