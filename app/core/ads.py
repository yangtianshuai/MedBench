# -*- coding: utf-8 -*-
"""ADS 数据源（A 路线）：本地 SQLite 宽表，供性能与口径验证。

为什么单独建这个文件：
    B 路线（直连 HIS）证明"能出数"，A 路线（ADS）证明"出得快"。
    两者共用同一份语义层，靠 data_source 切换 —— 如果切过去要改指标定义，
    就说明"物理映射 + 语义层分离"的设计失败了。

只读守门与 HIS 源同一套标准：非 SELECT 一律拒绝。
"""

from __future__ import annotations

import os
import re
import sqlite3
from typing import Any, Dict, List, Optional

from app.config import settings

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"attach|pragma|vacuum|replace)\b", re.I)


class ReadOnlyViolation(Exception):
    pass


def guard(sql: str) -> str:
    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        raise ReadOnlyViolation("空语句")
    low = s.lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise ReadOnlyViolation("只允许 SELECT / WITH 语句")
    if ";" in s:
        raise ReadOnlyViolation("检测到语句分隔符，拒绝执行多语句")
    if _FORBIDDEN.search(s):
        raise ReadOnlyViolation("检测到写操作或 DDL 关键字")
    return s


def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float, str, bytes)):
        return v
    return str(v)


def available() -> bool:
    return os.path.exists(settings.ads_path)


def query(sql: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """在 ADS 上执行只读查询。"""
    if not available():
        raise RuntimeError(f"ADS 不存在：{settings.ads_path}（先跑 scripts/build_ads.py）")
    s = guard(sql)
    limit = limit or settings.max_rows
    con = sqlite3.connect(settings.ads_path)
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


def tables() -> List[str]:
    if not available():
        return []
    con = sqlite3.connect(settings.ads_path)
    try:
        cur = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        return [r[0] for r in cur.fetchall()]
    finally:
        con.close()


def row_count(table: str = "dwd_fact_inpatient_discharge") -> Optional[int]:
    if not available() or table not in tables():
        return None
    con = sqlite3.connect(settings.ads_path)
    try:
        return con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    finally:
        con.close()
