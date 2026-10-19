# -*- coding: utf-8 -*-
"""Oracle 只读数据源（L1）。HIS 是默认源，LIS / EMR 等同构源复用本类。

多源接入在**抽取层**，不在查询层 —— 见 app/core/sources.py 的模块说明。

红线：
  1. 只执行 SELECT / WITH 开头、且不含分号拼接的单条语句
  2. 连接以只读方式打开，不提交任何事务
  3. 结果行数硬上限，防止一次问数拖垮镜像库

为什么不用 SQLAlchemy：oracledb 锁在 1.4.2（Oracle 11g + Instant Client 11.2），
与 SQLAlchemy 2.0 的 oracle 方言组合存在兼容风险，直接驱动更可控。
"""

from __future__ import annotations

import datetime as dt
import decimal
import os
import re
from typing import Any, Dict, List, Optional, Sequence

import oracledb

from app.config import settings

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|"
    r"commit|rollback|execute|call|begin|declare)\b", re.I)


class ReadOnlyViolation(RuntimeError):
    """只读守门拦截。"""


class OracleSource:
    """Oracle 只读数据源（按源名 + 连接参数构造）。

    只读红线（与初版一致，未放宽）：
      1. 只执行 SELECT / WITH 开头、且不含分号拼接的单条语句
      2. 连接以只读方式打开，不提交任何事务
      3. 结果行数硬上限，防止一次问数拖垮镜像库

    多源改造（v0.11）：连接参数可外部注入，同一套守门即可复用于 LIS / EMR 等源；
    不传 conn 时回落到 settings 的 HIS 配置 —— 原有单源行为完全不变。

    注意：新增数据源**不会**让问数引擎跨源查询。多源汇聚在抽取层，
    见 app/core/sources.py 的模块说明。
    """

    def __init__(self, name: str = "his", conn: Optional[Dict[str, Any]] = None,
                 refs: Optional[Dict[str, str]] = None):
        self.name = name
        self._cfg = dict(conn or {})
        self._refs = dict(refs or {})      # 字段 → 环境变量名（仅用于错误提示）
        self._conn = None

    def _params(self) -> Dict[str, Any]:
        c = self._cfg
        return {
            "host": c.get("host") or settings.his_host,
            "port": int(c.get("port") or settings.his_port),
            "service": c.get("service") or settings.his_service,
            "user": c.get("user") or settings.his_user,
            "password": c.get("password") or settings.his_password,
            "thick": bool(c.get("thick", settings.his_thick)),
        }

    # ── 守门 ──
    @staticmethod
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

    def connect(self):
        if self._conn is not None:
            return self._conn
        p = self._params()
        if not p["password"]:
            env = self._refs.get("password") or "ASK_HIS_PASSWORD"
            raise RuntimeError(
                f"数据源 '{self.name}' 未提供密码：请设置环境变量 {env}。"
                "问数服务必须使用独立只读账号，禁止使用带写权限的应用账号。")
        if p["thick"]:
            # Oracle 11g + Instant Client 11.2：必须 thick mode
            try:
                oracledb.init_oracle_client()
            except Exception:
                pass  # 已初始化时重复调用会抛错，忽略
        dsn = oracledb.makedsn(p["host"], p["port"], service_name=p["service"])
        self._conn = oracledb.connect(user=p["user"], password=p["password"], dsn=dsn)
        # 双保险：会话级只读
        try:
            cur = self._conn.cursor()
            cur.execute("SET TRANSACTION READ ONLY")
        except Exception:
            pass
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    # ── 查询 ──
    def query(self, sql: str, max_rows: Optional[int] = None,
              timeout: Optional[int] = None) -> List[Dict[str, Any]]:
        s = self.guard(sql)
        limit = max_rows or settings.max_rows
        conn = self.connect()
        cur = conn.cursor()
        try:
            cur.execute(s)
            cols = [d[0].lower() for d in cur.description] if cur.description else []
            rows: List[Dict[str, Any]] = []
            for i, raw in enumerate(cur):
                if i >= limit:
                    break
                rows.append({cols[j]: _norm(v) for j, v in enumerate(raw)})
            return rows
        finally:
            cur.close()

    def scalar(self, sql: str) -> Any:
        rows = self.query(sql, max_rows=1)
        return list(rows[0].values())[0] if rows else None

    def ping(self) -> Dict[str, Any]:
        """连接体检：版本 + 当前用户 + 是否可查。"""
        conn = self.connect()
        cur = conn.cursor()
        try:
            cur.execute("SELECT banner FROM v$version WHERE ROWNUM <= 1")
            version = cur.fetchone()[0]
            cur.execute("SELECT USER FROM dual")
            user = cur.fetchone()[0]
            p = self._params()
            return {"ok": True, "version": version.strip(), "user": user,
                    "dsn": f"{p['host']}:{p['port']}/{p['service']}"}
        finally:
            cur.close()


def _norm(v: Any) -> Any:
    if isinstance(v, (dt.datetime, dt.date)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, memoryview)):
        return None
    return v


# 向后兼容：保留原类名（from app.core.his import HisDataSource 仍可用）
HisDataSource = OracleSource
# 默认 HIS 单例：不传 conn → 回落 settings，行为与单源版本完全一致
his = OracleSource("his")
