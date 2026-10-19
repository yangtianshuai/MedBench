# -*- coding: utf-8 -*-
"""数据源注册与连接器工厂（F8.1）—— 多源可配。

设计原则：**多源接入在「抽取层」，不在「查询层」。**
  - 本模块只回答两件事：某个源「配好了没」、怎么连上它做只读访问。
  - 问数引擎**不经过**本模块 —— 它只面向单一出数源（ASK_DS = ads | his）。
    多源汇聚发生在抽取作业里（scripts/ → ADS 宽表），查询路径永远单源。
    理由：直连 20s vs ADS 52ms（343×）、权限围栏只需实现一次、
    源之间时效不同步（DRG 停 2019-06 / 报表层停 2024-10）、方言各异。
  - 未配置的源必须显式表达为 `unconfigured`，**不静默跳过、不阻塞其他源**。

密钥处理：`sources.yaml` 里只写「环境变量名」，本模块负责解引用。
  敏感字段一旦被写成字面值（或带上默认值），selftest 直接报错 —— 这是硬约束，
  因为「密钥明文落盘」已经犯过一次（docs/12 §1.2 S6）。

用法：
    python -m app.core.sources --selftest      # 自测
    python -m app.core.sources                 # 打印各源状态
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.config import ROOT, settings

_cache: Optional[Dict[str, Any]] = None
_connectors: Dict[str, Any] = {}
_lock = threading.Lock()

# 敏感字段：只允许写环境变量名，且不允许默认值
SECRET_FIELDS = ("password", "passwd", "pwd", "secret", "token", "api_key")

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

_INT_FIELDS = ("port",)
_BOOL_FIELDS = ("thick", "enable", "ssl", "trusted")


class SourceUnavailable(RuntimeError):
    """数据源不可用（未登记 / 未启用 / 缺环境变量 / 类型不支持）。"""


# ────────────────────────────────────────────────────────────
# 加载与解析
# ────────────────────────────────────────────────────────────
def load(force: bool = False) -> Dict[str, Any]:
    global _cache
    if _cache is None or force:
        p = Path(settings.sources_path)
        if not p.exists():
            _cache = {"sources": {}}
        else:
            with p.open("r", encoding="utf-8") as f:
                _cache = yaml.safe_load(f) or {"sources": {}}
    return _cache


def get(name: str) -> Optional[Dict[str, Any]]:
    return (load().get("sources") or {}).get(name)


def all_sources() -> Dict[str, Any]:
    return dict(load().get("sources") or {})


def _cast(field: str, val: Any) -> Any:
    """按字段名把字符串值转成合适类型；相对路径按项目根解析。"""
    if isinstance(val, bool) or val is None:
        return val
    if field in _INT_FIELDS:
        try:
            return int(val)
        except (TypeError, ValueError):
            return val
    if field in _BOOL_FIELDS:
        return str(val).strip().lower() in ("1", "true", "yes", "y", "on")
    if field == "path" and isinstance(val, str) and not os.path.isabs(val):
        return os.path.join(str(ROOT), val)
    return val


def _resolve_field(field: str, spec: Any, missing: List[str]) -> Any:
    """解析一个连接字段。

    spec 三种写法：
      password: ASK_HIS_PASSWORD                     # 只从环境变量取，无默认
      host: {env: ASK_HIS_HOST, default: 10.1.33.46} # 环境变量优先，可带默认
      thick: true                                    # 字面量直接用
    """
    if isinstance(spec, dict):
        env_name = spec.get("env")
        has_default = "default" in spec
        default = spec.get("default")
    elif isinstance(spec, str):
        env_name, has_default, default = spec, False, None
    else:
        return _cast(field, spec)

    if env_name:
        raw = os.getenv(env_name)
        if raw not in (None, ""):
            return _cast(field, raw)
    if has_default:
        return _cast(field, default)
    if env_name:
        missing.append(env_name)
    return None


def resolve_conn(name: str) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """把源配置解析成实际连接参数，返回 (conn, 不可用原因列表)。

    conn 为 None 表示该源不可用；原因用**人话**给出（缺哪个环境变量、
    还是根本没启用），而不是一句「跳过」—— 静默跳过会让「没接入」看起来像「接入失败」。
    """
    src = get(name)
    if not src:
        return None, [f"源 '{name}' 未在 sources.yaml 中登记"]
    if not src.get("enabled", False):
        return None, [f"源 '{name}' 未启用（enabled: false）"]

    missing: List[str] = []
    conn = {k: _resolve_field(k, v, missing)
            for k, v in (src.get("conn") or {}).items()}
    if missing:
        return None, [f"缺少环境变量 {m}" for m in missing]
    return conn, []


def env_refs(name: str) -> Dict[str, str]:
    """该源各字段对应的环境变量名（用于错误提示，不含任何值）。"""
    src = get(name) or {}
    out: Dict[str, str] = {}
    for field, spec in (src.get("conn") or {}).items():
        env = spec.get("env") if isinstance(spec, dict) else (spec if isinstance(spec, str) else None)
        if env:
            out[field] = env
    return out


def status() -> List[Dict[str, Any]]:
    """各源的注册与就绪状态（供管理端）。

    ★ 只回显字段名，**绝不回显任何连接值**（尤其密码）。
    """
    out: List[Dict[str, Any]] = []
    for name, src in all_sources().items():
        conn, missing = resolve_conn(name)
        enabled = bool(src.get("enabled"))
        out.append({
            "source": name,
            "name": src.get("name"),
            "kind": src.get("kind"),
            "layer": src.get("layer"),
            "enabled": enabled,
            "configured": conn is not None,
            "state": ("ready" if conn is not None
                      else ("disabled" if not enabled else "unconfigured")),
            "missing": missing,
            "covers": src.get("covers"),
            "conn_fields": sorted((src.get("conn") or {}).keys()),
            "note": (src.get("note") or "").strip(),
        })
    return sorted(out, key=lambda x: (x.get("layer") or "", x["source"]))


def by_layer(layer: str) -> List[str]:
    return [s["source"] for s in status() if s.get("layer") == layer]


# ────────────────────────────────────────────────────────────
# 连接器
# ────────────────────────────────────────────────────────────
class SqliteSource:
    """SQLite 只读源（ADS 这类文件型源）。

    守门规则直接复用 app.core.ads 的实现 —— 只读标准不允许有两套。
    """

    def __init__(self, name: str, conn: Dict[str, Any]):
        self.name = name
        self.path = conn.get("path") or settings.ads_path

    def query(self, sql: str, max_rows: Optional[int] = None,
              timeout: Optional[int] = None) -> List[Dict[str, Any]]:
        from app.core.ads import _norm, guard
        s = guard(sql)
        if not os.path.exists(self.path):
            raise RuntimeError(
                f"数据源 '{self.name}' 的文件不存在：{self.path}（先跑抽取作业建 ADS）")
        limit = max_rows or settings.max_rows
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        try:
            cur = con.execute(s)
            rows: List[Dict[str, Any]] = []
            for i, r in enumerate(cur):
                if i >= limit:
                    break
                rows.append({k: _norm(r[k]) for k in r.keys()})
            return rows
        finally:
            con.close()

    def ping(self) -> Dict[str, Any]:
        if not os.path.exists(self.path):
            raise RuntimeError(f"文件不存在：{self.path}")
        con = sqlite3.connect(self.path)
        try:
            n = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            return {"ok": True, "name": self.name, "path": self.path,
                    "tables": len(n)}
        finally:
            con.close()

    def close(self) -> None:
        return None


def connector(name: str):
    """取得某源的只读连接器（带缓存）。不可用则抛 SourceUnavailable。"""
    src = get(name) or {}
    conn, missing = resolve_conn(name)
    if conn is None:
        raise SourceUnavailable(f"数据源 '{name}' 不可用：" + "；".join(missing))

    with _lock:
        got = _connectors.get(name)
        if got is not None:
            return got
        kind = src.get("kind") or "oracle"
        if kind == "oracle":
            from app.core.his import OracleSource
            c = OracleSource(name, conn, refs=env_refs(name))
        elif kind == "sqlite":
            c = SqliteSource(name, conn)
        else:
            raise SourceUnavailable(
                f"数据源 '{name}' 的 kind='{kind}' 尚未支持（已支持：oracle / sqlite）")
        _connectors[name] = c
        return c


def close_all() -> None:
    with _lock:
        for c in _connectors.values():
            try:
                c.close()
            except Exception:
                pass
        _connectors.clear()


# ────────────────────────────────────────────────────────────
# 自测
# ────────────────────────────────────────────────────────────
def selftest() -> bool:
    """运行：python -m app.core.sources --selftest"""
    reg = all_sources()
    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    check(f"sources.yaml 可解析（{len(reg)} 个源）", len(reg) > 0)
    check(f"声明了出数源（layer: serve）",
          bool(by_layer("serve")), "、".join(by_layer("serve")))

    # ★ 最重要的一条：敏感字段不得写明文、不得带默认值
    offenders: List[str] = []
    for name, src in reg.items():
        for field, spec in (src.get("conn") or {}).items():
            if field not in SECRET_FIELDS:
                continue
            if isinstance(spec, dict):
                env = spec.get("env")
                if "default" in spec:
                    offenders.append(f"{name}.{field}(带默认值)")
            else:
                env = spec
            if not (isinstance(env, str) and _ENV_NAME.match(env)):
                offenders.append(f"{name}.{field}")
    check("敏感字段只写环境变量名、且无默认值", not offenders,
          "、".join(offenders) if offenders else "0 处")

    # 状态计算不得抛异常
    try:
        st = status()
        ok = True
    except Exception as e:                                  # pragma: no cover
        st, ok = [], False
        print(f"      {type(e).__name__}: {e}")
    check("源状态可计算（不抛异常）", ok)

    disabled = [s["source"] for s in st if not s["enabled"]]
    check(f"未启用的源标为 disabled（{len(disabled)} 个）",
          all(s["state"] == "disabled" for s in st if not s["enabled"]),
          "、".join(disabled))

    # 启用但缺配置 → 必须给出缺失项（可解释），不能只剩一句「跳过」
    vague = [s["source"] for s in st
             if s["enabled"] and not s["configured"] and not s["missing"]]
    check("未配置的源必须给出缺失项", not vague,
          "、".join(vague) if vague else "0 处")

    ready = [s["source"] for s in st if s["configured"]]
    check(f"已就绪的源（{len(ready)} 个）", bool(ready), "、".join(ready))

    # 域 → 源映射必须完整（source_state 以 domain 为主键，故域名必须唯一）
    dom_path = Path(settings.source_domains_path)
    domains = []
    if dom_path.exists():
        with dom_path.open("r", encoding="utf-8") as f:
            domains = (yaml.safe_load(f) or {}).get("domains") or []

    names = [d.get("domain") for d in domains]
    dup = sorted({n for n in names if names.count(n) > 1})
    check(f"域名唯一（{len(domains)} 个域）", not dup, "、".join(dup) if dup else "")

    unknown = sorted({d.get("source", "his") for d in domains
                      if d.get("source", "his") not in reg})
    check("每个域都指向已登记的源", not unknown,
          "、".join(unknown) if unknown else "")

    print("=" * 60)
    print("自测结果: " + ("全部通过" if passed else "存在失败"))
    return passed


def _print_status() -> None:
    for s in status():
        flag = {"ready": "✅", "disabled": "⏸", "unconfigured": "⚠️"}.get(s["state"], "?")
        extra = ("  缺：" + "、".join(s["missing"])) if s["missing"] else ""
        print(f"{flag} {s['source']:<10} {s['layer'] or '-':<8} {s['kind'] or '-':<8} "
              f"{s['name'] or ''}{extra}")


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        sys.exit(0 if selftest() else 1)
    _print_status()
