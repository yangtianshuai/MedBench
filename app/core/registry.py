# -*- coding: utf-8 -*-
"""语义层加载与指标资产服务（F5）。

指标定义是**人工资产**：任何变更都要留痕（F5.2），且必须经审批流程。
本模块只负责读写 YAML 与版本留痕，不做口径推导（那是 LLM 的禁区）。
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.config import settings
from app.core import meta_db

_lock = threading.Lock()
_cache: Dict[str, Any] = {}


def _read_yaml(path: str) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"语义层文件不存在: {path}")
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_registry(force: bool = False) -> Dict[str, Any]:
    """加载指标注册表（含进程内缓存，支持热更新）。"""
    from app.core.physical_map import apply as _apply_pm
    from app.core.physical_map import apply_profile_only as _apply_profile
    with _lock:
        if force or "registry" not in _cache:
            raw = _read_yaml(settings.registry_path)
            if settings.data_source == "ads":
                # ADS 宽表的表名/列名基本就是逻辑名，**不做 HIS 表映射**；
                # 但仍要应用 ADS 自己的物理映射（physical_map_ads.yaml）——
                # 语义层声明了、宽表没物化的列（三种科室口径、院区）在那里声明落回哪一列。
                # 少了这一步，任何**科室级授权**的用户在 A 路线上都会拿到
                # `no such column: belong_dept_code`（全院范围的人无感，因为 RLS 不注入）。
                # 院方业务配置（科室分组、口径规则）与数据源无关，照常应用。
                _cache["registry"] = _apply_profile(raw)
            else:
                _cache["registry"] = _apply_pm(raw)
            _cache["registry_mtime"] = os.path.getmtime(settings.registry_path)
        return _cache["registry"]


def load_ambiguity(force: bool = False) -> Dict[str, Any]:
    with _lock:
        if force or "amb" not in _cache:
            _cache["amb"] = _read_yaml(settings.ambiguity_path)
        return _cache["amb"]


def registry_version() -> str:
    return str(load_registry().get("version", "unknown"))


# ── 指标查询 ──
def list_metrics(domain: Optional[str] = None, keyword: Optional[str] = None) -> List[Dict[str, Any]]:
    reg = load_registry()
    out = []
    for m in reg.get("metrics") or []:
        if domain and m.get("domain") != domain:
            continue
        if keyword:
            hay = " ".join(str(m.get(k, "")) for k in ("code", "name", "definition")) \
                  + " " + " ".join(m.get("alias") or [])
            if keyword not in hay:
                continue
        out.append({
            "code": m.get("code"),
            "name": m.get("name"),
            "type": m.get("type"),
            "domain": m.get("domain"),
            "definition": m.get("definition"),
            "formula": m.get("formula"),
            "owner": m.get("owner"),
            "status": m.get("status", "online"),
            # 可切换口径来自 measure（见 caliber_options 的说明），不是 metric 自身的字段
            **caliber_options(m.get("code")),
            # ★ 该指标声明支持的分组/下钻维度（后端判权限，前端只是展示）
            "dimensions": list(m.get("dimensions") or []),
            "sensitivity": m.get("sensitivity", "public"),
        })
    return out


def get_metric(code: str) -> Optional[Dict[str, Any]]:
    for m in load_registry().get("metrics") or []:
        if m.get("code") == code:
            return m
    return None


def caliber_options(metric_code: str) -> Dict[str, List[str]]:
    """该指标**可切换**的时间口径 / 科室口径（F2.2 口径抽屉的可选项）。

    ★ 为什么不能只读 metric 自身：口径的可选值定义在 **measure** 上
      （`date_basis: {default, supported, expr_map}`），metric 只写
      `default_date_basis`。此前界面读的是 metric 的 `supported_*`，
      永远是空 —— 于是"口径切换"没有任何可选项，
      用户只能换个说法重问（F2.2 形同虚设，且重问会绕开口径写回）。
    """
    reg = load_registry()
    m = next((x for x in (reg.get("metrics") or []) if x.get("code") == metric_code), None)
    if not m:
        return {"supported_date_basis": [], "supported_dept_basis": []}
    meas = next((x for x in (reg.get("measures") or [])
                 if x.get("code") == m.get("measure")), None) or {}
    db = meas.get("date_basis") if isinstance(meas.get("date_basis"), dict) else {}
    kb = meas.get("dept_basis") if isinstance(meas.get("dept_basis"), dict) else {}

    def opts(own: Any, spec: Dict[str, Any]) -> List[str]:
        if own:
            return list(own)
        if spec.get("supported"):
            return list(spec["supported"])
        return [spec["default"]] if spec.get("default") else []

    return {"supported_date_basis": opts(m.get("supported_date_basis"), db),
            "supported_dept_basis": opts(m.get("supported_dept_basis"), kb)}


def metric_domains() -> List[str]:
    return sorted({m.get("domain") for m in load_registry().get("metrics") or []
                   if m.get("domain")})


# ── 指标注册 / 变更（F5.1 / F5.2）──
def upsert_metric(metric: Dict[str, Any], operator: str, change_note: str = "") -> Dict[str, Any]:
    """新增或更新指标定义。

    ⚠️ 指标口径是人工资产：这里只做「写入 + 留痕」，不校验业务正确性。
       正式环境应改为 Git PR 流程（指标即代码），本实现为 MVP 的简化路径。

    ★ **合并语义，不是替换**：本接口的字段集来自 `models.MetricIn`（界面上能填的那些），
      而 index 里的 `measure` / `dimensions` / `default_date_basis` / `default_dept_basis`
      **不在其中**。若整条替换，用户在界面上只改一个「名称」，
      就会把指标与度量的绑定、可用维度一起悄悄删掉 —— 指标立刻变成查不出数的僵尸条目，
      而版本记录里只会写"改名"。因此规则是：值为 `None` = 未提供 = 保留原值；
      `[]` / `""` 是显式清空，照旧生效。
    """
    reg = load_registry()
    metrics: List[Dict[str, Any]] = list(reg.get("metrics") or [])
    code = metric.get("code")
    if not code:
        raise ValueError("指标缺少 code")

    prev = next((m for m in metrics if m.get("code") == code), None)
    changed: List[str] = []
    if prev is not None:
        merged = dict(prev)
        for k, v in metric.items():
            if v is None:
                continue
            if prev.get(k) != v:
                changed.append(k)
            merged[k] = v
        metrics[metrics.index(prev)] = merged
        action = "update"
    else:
        # 新建才补默认值：更新路径上的 None 一律表示"没说，别动"
        # （`sensitivity` 给 public 是 fail-closed 的方向：公开不放大范围；要收紧就在界面上选）
        for k, dv in (("type", "count"), ("sensitivity", "public"), ("status", "online")):
            if metric.get(k) is None:
                metric[k] = dv
        metrics.append(metric)
        changed = sorted(metric.keys())
        action = "create"

    reg["metrics"] = metrics
    _write_registry(reg)

    # 版本记录只记"这条指标动过"，具体动了哪些字段放在返回值里给界面回显
    version = f"{len(meta_db.list_metric_versions(code)) + 1}.0"
    meta_db.log_metric_version(
        metric_code=code, version=version, change_note=change_note or action,
        operator=operator, definition=str(metric.get("definition", ""))[:500])
    return {"action": action, "code": code, "version": version, "changed": changed}


def retire_metric(code: str, operator: str, reason: str = "") -> Dict[str, Any]:
    """指标下线（F5.6）：争议指标先下线，不硬撑（08 R1）。"""
    reg = load_registry()
    metrics = list(reg.get("metrics") or [])
    for m in metrics:
        if m.get("code") == code:
            m["status"] = "offline"
            m["offline_reason"] = reason
            reg["metrics"] = metrics
            _write_registry(reg)
            meta_db.log_metric_version(code, "offline", reason or "下线", operator, "")
            return {"code": code, "status": "offline"}
    raise KeyError(f"指标不存在: {code}")


def _write_registry(reg: Dict[str, Any]) -> None:
    """写回 YAML。

    ⚠️ safe_dump 会丢失原文件注释（PyYAML 限制）。正式环境建议改为
       ruamel.yaml 或走 Git PR，保留评审痕迹。
    """
    path = Path(settings.registry_path)
    backup = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        backup.write_bytes(path.read_bytes())
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(reg, f, allow_unicode=True, sort_keys=False)
    load_registry(force=True)
