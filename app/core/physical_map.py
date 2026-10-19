# -*- coding: utf-8 -*-
"""物理映射（F5.3）：把语义层 YAML 里的逻辑表/字段替换为 HIS 物理表/字段。

设计原则：
  1. 语义层 YAML（metric_registry.yaml）保持医院无关，只用逻辑名
  2. 物理映射 YAML（physical_map_*.yaml）每院一套
  3. 注册表加载时把物理映射叠加上去，SQL 生成器**不感知**物理表名
  4. 这样接入新医院只改映射配置，指标定义零改动

接入新医院时：
    - 复制 physical_map_his.yaml 为 physical_map_新院.yaml
    - 改表名/字段映射
    - 设 ASK_PHYSICAL_MAP=semantic-layer/physical_map_新院.yaml
"""

from __future__ import annotations

import logging
import os
import threading
from copy import deepcopy
from typing import Any, Dict, Iterable, List, Optional

import yaml

from app.config import settings

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_map: Optional[Dict[str, Any]] = None
_map_mtime: float = 0.0
_ads_map: Optional[Dict[str, Any]] = None
_ads_mtime: float = 0.0
_profile: Optional[Dict[str, Any]] = None
_profile_mtime: float = 0.0


def load(force: bool = False) -> Dict[str, Any]:
    global _map, _map_mtime
    with _lock:
        path = settings.physical_map_path
        if not os.path.exists(path):
            _map = {}
            return _map
        mtime = os.path.getmtime(path)
        if force or _map is None or mtime != _map_mtime:
            with open(path, "r", encoding="utf-8") as f:
                _map = yaml.safe_load(f) or {}
            _map_mtime = mtime
            _log.info("loaded physical map: %s (dialect=%s, tables=%d)",
                      path, _map.get("dialect"), len(_map.get("tables") or {}))
        return _map


def load_profile(force: bool = False) -> Dict[str, Any]:
    """加载院方业务配置（科室分组 + 口径规则）。

    与物理映射分开成两个文件，是因为关注点不同：
    物理映射是技术映射（表名/字段名），院方业务配置是业务口径
    （哪些科室算一组、哪些患者算不算），后者才是管理员日常要改的。
    """
    global _profile, _profile_mtime
    with _lock:
        path = settings.hospital_profile_path
        if not os.path.exists(path):
            _profile = {}
            return _profile
        mtime = os.path.getmtime(path)
        if force or _profile is None or mtime != _profile_mtime:
            with open(path, "r", encoding="utf-8") as f:
                _profile = yaml.safe_load(f) or {}
            _profile_mtime = mtime
            _log.info("loaded hospital profile: %s (groups=%d, rules=%d)",
                      path, len(_profile.get("dept_groups") or []),
                      len(_profile.get("caliber_rules") or []))
        return _profile


def apply(reg: Dict[str, Any]) -> Dict[str, Any]:
    """对一份语义层 registry 应用物理映射，返回新 dict（不修改入参）。"""
    pm = load()
    if not pm:
        return reg
    reg = deepcopy(reg)

    tables = pm.get("tables") or {}
    columns = pm.get("columns") or {}
    exprs = pm.get("exprs") or {}
    date_basis_map = pm.get("date_basis") or {}
    dept_basis_map = pm.get("dept_basis") or {}
    fail_list = set(pm.get("fail_if_unmapped") or [])
    measure_over = pm.get("measures") or {}

    # 列名替换（与 ADS 路线共用同一套规则；measure 级的覆盖在下面最后应用）
    _apply_columns(reg, columns, exprs)
    _mark_absent(reg, columns)
    # JOIN 关系与 join 模式维度（只在 resolve: join 时参与生成，fact 模式零影响）
    _apply_relations(reg, tables, columns)
    _apply_join_dimensions(reg, tables, columns)

    for m in reg.get("measures") or []:
        code = m.get("code", "")
        if code in fail_list:
            # 显式标记为暂未支持的 measure：表名置为 None 让 SQL 生成失败并给出明确错误
            m["_unmapped"] = True
            m["_unmapped_reason"] = f"measure {code} 在当前物理映射中未启用"
            continue

        # 表名替换
        t = m.get("table")
        if t in tables:
            m["table"] = tables[t]

        # date_basis.expr_map / dept_basis.expr_map 的**按口径**兜底
        # （列名替换已在循环外统一做过；这里处理"该口径在物理层没有列，
        #   但整条口径要换成另一个列"的情况）
        for k, v in (m.get("date_basis") or {}).get("expr_map", {}).items():
            if v in columns:
                m["date_basis"]["expr_map"][k] = columns[v]
            elif k in date_basis_map:
                m["date_basis"]["expr_map"][k] = date_basis_map[k]
        for k, v in (m.get("dept_basis") or {}).get("expr_map", {}).items():
            if v in columns:
                m["dept_basis"]["expr_map"][k] = columns[v]
            elif k in dept_basis_map:
                m["dept_basis"]["expr_map"][k] = dept_basis_map[k]

        # ── 按 measure 的覆盖（最后应用，优先级最高）──
        # 为什么需要：同一个逻辑事实表，在不同医院里可能被拆成多张物理表。
        # 本例：住院就诊在 MEDREC.PAT_VISIT，而费用在 INPBILL.INP_BILL_DETAIL，
        # 两者都是逻辑上的 dwd_fact_inpatient_discharge，但物理上不是一张表。
        ov = measure_over.get(code) or {}
        if ov.get("table"):
            m["table"] = ov["table"]
        if ov.get("expr"):
            m["expr"] = ov["expr"]
        for basis_key, target in (("date_basis", m.get("date_basis")),
                                  ("dept_basis", m.get("dept_basis"))):
            ov_map = ov.get(basis_key) or {}
            if ov_map and isinstance(target, dict):
                target.setdefault("expr_map", {}).update(ov_map)
        if ov.get("dedup_key"):
            m["dedup_key"] = ov["dedup_key"]

    # 同时标记 metrics 引用了 _unmapped measure
    unmapped_codes = {m.get("code") for m in (reg.get("measures") or [])
                      if m.get("_unmapped")}
    for mc in reg.get("metrics") or []:
        meas = mc.get("measure")
        if meas in unmapped_codes:
            mc["_unmapped"] = True

    # ── 院方业务配置：口径规则 + 科室分组 ──
    prof = load_profile()
    if prof:
        _apply_caliber_rules(reg, prof, colmap=columns, data_source="his")
        _apply_dept_groups(reg, prof)

    return reg


def load_ads(force: bool = False) -> Dict[str, Any]:
    """加载 ADS 路线的物理映射（`physical_map_ads.yaml`）。

    与 HIS 那份分开，是因为两份映射回答的是**不同的问题**：
    HIS 那份是"逻辑名 → HIS 物理表/列"，ADS 这份只回答
    "语义层声明了、宽表没物化的列该落回哪一列"。
    """
    global _ads_map, _ads_mtime
    with _lock:
        path = settings.physical_map_ads_path
        if not os.path.exists(path):
            _ads_map = {}
            return _ads_map
        mtime = os.path.getmtime(path)
        if force or _ads_map is None or mtime != _ads_mtime:
            with open(path, "r", encoding="utf-8") as f:
                _ads_map = yaml.safe_load(f) or {}
            _ads_mtime = mtime
            _log.info("loaded ADS physical map: %s (columns=%d)",
                      path, len(_ads_map.get("columns") or {}))
        return _ads_map


def apply_profile_only(reg: Dict[str, Any]) -> Dict[str, Any]:
    """ADS 路线：应用 ADS 物理映射 + 院方业务配置（**不做 HIS 表映射**）。

    ADS 宽表的表名/列名基本就是语义层的逻辑名 —— 但"基本"不等于"全部"：
    语义层声明的三种科室口径与院区列在宽表里并没有物化。少了这一步，
    任何**科室级授权**的用户在 A 路线上都会拿到 `no such column`，
    而全院范围的用户一切正常（RLS 对 `["*"]` 不注入）——
    最容易漏测的一类故障。

    科室分组与口径规则**与数据源无关，必须照常应用**，
    否则同一个问题在两条路线上会给出不同的数，而且不会报任何错。
    """
    cols = dict(load_ads().get("columns") or {})
    prof = load_profile()
    if not cols and not prof:
        return reg
    reg = deepcopy(reg)
    _apply_columns(reg, cols)
    _mark_absent(reg, cols)
    # ADS 宽表里维表通常与事实同库同名；仍走同一套映射逻辑，
    # 免得出现"HIS 那边改了、ADS 这边没改"的半边偏差。
    _apply_relations(reg, {}, cols)
    _apply_join_dimensions(reg, {}, cols)
    if prof:
        _apply_caliber_rules(reg, prof, colmap=cols, data_source="ads")
        _apply_dept_groups(reg, prof)
    return reg


def _mark_absent(reg: Dict[str, Any], columns: Dict[str, Any]) -> None:
    """把映射里显式写成 `null` 的列记进 `reg["_absent_columns"]`。

    `campus_code: null` 的意思是"**这个数据源没有这一列**"，而不是"没配"。
    区别很重要：没配 → 保留逻辑名（拼出一条必然报错的 SQL）；显式 null →
    引擎可以**在生成 SQL 之前**就报出可诊断的错误，而不是让 DBA 去看
    `no such column` / `ORA-00904` 猜是哪个环节。
    """
    absent = sorted(k for k, v in (columns or {}).items() if v is None)
    if absent:
        reg["_absent_columns"] = absent


def _apply_columns(reg: Dict[str, Any], columns: Dict[str, str],
                   exprs: Optional[Dict[str, str]] = None) -> None:
    """把逻辑列名替换成目标数据源上的实际列名（就地修改 reg）。

    单抽成一个函数，是为了让两条路线**共用同一套替换规则**：
    各写一份的话，早晚会出现"HIS 改了、ADS 没改"这类只在一半场景下发作的偏差。
    """
    if not columns and not exprs:
        return
    exprs = exprs or {}
    # 值为 null 的条目表示"该源没有这一列"，不是"替换成空"（见 _mark_absent）
    cmap = {k: v for k, v in (columns or {}).items() if v}
    for m in reg.get("measures") or []:
        e = m.get("expr")
        if e:
            if e in exprs:
                m["expr"] = exprs[e]
            elif e in cmap:
                m["expr"] = cmap[e]
        for k, v in (m.get("date_basis") or {}).get("expr_map", {}).items():
            if isinstance(v, str) and v in cmap:
                m["date_basis"]["expr_map"][k] = cmap[v]
        for k, v in (m.get("dept_basis") or {}).get("expr_map", {}).items():
            if isinstance(v, str) and v in cmap:
                m["dept_basis"]["expr_map"][k] = cmap[v]
        if m.get("dedup_key") in cmap:
            m["dedup_key"] = cmap[m["dedup_key"]]
        for f in m.get("filters") or []:
            if f.get("field") in cmap:
                f["field"] = cmap[f["field"]]


def _apply_relations(reg: Dict[str, Any], tables: Dict[str, str],
                     columns: Dict[str, str]) -> None:
    """把 relations 里的逻辑表名/连接列换成物理名（就地修改）。

    只在维度声明 `resolve: join` 时才影响 SQL；`resolve: fact` 时这段声明不参与生成。
    连接条件的 left 是事实侧列、right 是维表侧列，两者共用同一张列映射表 ——
    各院维表的列名通常与事实侧不同（DEPT_CODE / DEPT_ID），配映射时覆盖即可。
    """
    rels = reg.get("relations") or []
    if not rels:
        return
    cmap = {k: v for k, v in (columns or {}).items() if v}
    tmap = {k: v for k, v in (tables or {}).items() if v}
    for r in rels:
        if r.get("from") in tmap:
            r["from"] = tmap[r["from"]]
        if r.get("to") in tmap:
            r["to"] = tmap[r["to"]]
        conds: List[Dict[str, Any]] = []
        # join_on 是正式字段；"on" / True 兼容手写旧配置（YAML 1.1 会把 on: 解析成 True）
        when = r.get("join_on", r.get("on", r.get(True)))
        if isinstance(when, dict):
            conds.extend(v for v in when.values() if isinstance(v, dict))
        if isinstance(r.get("on_default"), dict):
            conds.append(r["on_default"])
        for c in conds:
            if c.get("left") in cmap:
                c["left"] = cmap[c["left"]]
            if c.get("right") in cmap:
                c["right"] = cmap[c["right"]]


def _apply_join_dimensions(reg: Dict[str, Any], tables: Dict[str, str],
                           columns: Dict[str, str]) -> None:
    """只有 `resolve: join` 的维度做表名/列名替换。

    为什么不给所有维度都替换：`resolve: fact` 时维度列是事实表上的列，
    它们的映射由 measures 那条链路负责；这里再动一次会双重替换，
    可能出现「同一个逻辑列被映成两个不同物理列」的隐性偏差。
    """
    dims = reg.get("dimensions") or []
    if not dims:
        return
    cmap = {k: v for k, v in (columns or {}).items() if v}
    tmap = {k: v for k, v in (tables or {}).items() if v}
    for d in dims:
        if (d.get("resolve") or "fact") != "join":
            continue
        if d.get("table") in tmap:
            d["table"] = tmap[d["table"]]
        for k in ("expr", "label_expr"):
            if d.get(k) in cmap:
                d[k] = cmap[d[k]]


def _render_where(where: str, colmap: Dict[str, str]) -> str:
    """把规则里的 {逻辑列名} 替换成目标列名。

    规则不写死物理列名，是因为同一条规则要同时服务两个数据源：
    直连 HIS 的列叫 NEWBORN，ADS 宽表的列叫 newborn。写死任何一边，
    另一边就会静默失效（症状是「配了规则却没变化」）。
    """
    import re
    return re.sub(r"\{(\w+)\}",
                  lambda m: colmap.get(m.group(1), m.group(1)), where)


def _apply_caliber_rules(reg: Dict[str, Any], prof: Dict[str, Any],
                         colmap: Optional[Dict[str, str]] = None,
                         data_source: str = "his") -> None:
    """把「已启用」的口径规则挂到对应 measure 上（一段 WHERE 片段）。

    为什么不复用 measure.filters：filters 只支持 field/op/value 三元式，
    表达不了 `NEWBORN IS NULL OR NEWBORN NOT IN (...)` 这种带 NULL 语义的条件。
    """
    colmap = colmap or {}
    enabled = []
    for r in (prof.get("caliber_rules") or []):
        if not r.get("enabled"):
            continue
        if r.get("his_only") and data_source != "his":
            # 规则引用了 HIS 专有对象（如 COMM.DEPT_DICT），换数据源就跑不了。
            # 明确跳过并告警，好过拼出一条必然报错的 SQL。
            _log.warning("口径规则 %s 标记 his_only，数据源 %s 不适用，已跳过",
                         r.get("code"), data_source)
            continue
        enabled.append(r)
    if not enabled:
        return
    by_measure: Dict[str, List[str]] = {}
    for r in enabled:
        for code in (r.get("applies_to") or []):
            by_measure.setdefault(code, []).append(r["where"])
    for m in reg.get("measures") or []:
        ws = by_measure.get(m.get("code"))
        if ws:
            m["_caliber_where"] = [_render_where(w, colmap) for w in ws]


def _apply_dept_groups(reg: Dict[str, Any], prof: Dict[str, Any]) -> None:
    """把科室分组配置翻译成维度（dept_group）的 CASE 表达式。

    分组规则由院方定义，列名却随指标口径变（出院科/入院科/开单科…），
    所以用 `{discharge}` 占位符，SQL 生成时按该 measure 的科室列替换。
    """
    for g in (prof.get("dept_groups") or []):
        code = g.get("code")
        groups = g.get("groups") or {}
        if not code or not groups:
            continue
        basis_key = g.get("from_dept_basis") or "discharge"
        parts = []
        for name, depts in groups.items():
            if not depts:
                continue
            inlist = ", ".join(f"'{d}'" for d in depts)
            parts.append(f"WHEN {{{basis_key}}} IN ({inlist}) THEN '{name}'")
        if not parts:
            continue
        case_expr = ("CASE " + " ".join(parts)
                     + f" ELSE '{g.get('default') or '未分组'}' END")
        dims = reg.setdefault("dimensions", [])
        if any(d.get("code") == code for d in dims):
            continue
        dims.append({
            "code": code,
            "name": g.get("name") or code,
            "type": "categorical",
            "case_expr": case_expr,
            "case_basis": basis_key,
            "alias": [g.get("name") or code, "科室分组"],
            "note": g.get("source") or "",
        })

        # 分组维度对所有指标开放：它是院方配出来的观察视角，
        # 不该要求每个指标在语义层里各声明一遍（否则加一个分组要改几十处指标定义）。
        for mc in reg.get("metrics") or []:
            dl = mc.get("dimensions") or []
            if code not in dl:
                mc["dimensions"] = dl + [code]
