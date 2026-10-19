# -*- coding: utf-8 -*-
"""源域时效探测（F8.2）+ 停更标记（F2.3 的 source_stale）。

实测教训：本 HIS 存在"半死"数据域——DRG_DETAIL 止于 2019-06、REPORT 层止于 2024-10。
若不探测并标记，用户会把 7 年前的数据当现状，这比"未结算虚假下跌"更严重。

多源（v0.11）：每个域声明自己属于哪个源（`source:` 字段，缺省 his），
探测时按源取连接器。未接入的源记为 `unconfigured`，与「接入失败」区分开。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from app.config import settings
from app.core import meta_db
from app.core.sources import SourceUnavailable, connector


def load_domains() -> List[Dict[str, Any]]:
    p = Path(settings.source_domains_path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("domains") or []


def classify(last_date: Optional[date], today: Optional[date] = None) -> str:
    """时效分级。

    为什么要单独区分 delayed（7~45 天）：
    实测门诊域滞后 29 天却会被判成 t_plus_1，用户完全看不出数据已经旧了近一个月。
    滞后必须显式表达，否则"看起来正常"比"停更"更危险。
    """
    today = today or date.today()
    if last_date is None:
        return "unknown"
    gap = (today - last_date).days
    if gap <= 1:
        return "streaming"
    if gap <= 7:
        return "t_plus_1"
    if gap > settings.stale_after_days:
        return "stale"
    return "delayed"


def probe_domain(d: Dict[str, Any]) -> Dict[str, Any]:
    """探测单个域的最新数据日期。只读，且只查 MAX(日期列)。

    ★ 按「域所属的源」路由（v0.11）：此前写死 `his.query`，
      等于假设所有域都在 HIS 实例里 —— 接入 LIS / EMR 时必然静默失效
      （配置改了却不生效，且不报错）。
    """
    domain = d["domain"]
    source = d.get("source", "his")        # 向后兼容：未标注 source 的域默认 HIS
    table, col = d["table"], d["date_col"]

    try:
        conn = connector(source)
    except SourceUnavailable as e:
        # ★ 不可用必须显式记为 unconfigured：不静默跳过、不阻塞其他源。
        #   「没接入」与「接入失败」是两件事，不能都变成一句「跳过」。
        meta_db.upsert_source_state(domain, table, col, None, "unconfigured",
                                    str(e)[:200], source=source)
        return {"domain": domain, "source": source, "freshness": "unconfigured",
                "error": str(e)[:200]}

    sql = f"SELECT MAX({col}) FROM {table}"
    try:
        rows = conn.query(sql, max_rows=1)
        raw = list(rows[0].values())[0] if rows else None
    except Exception as e:
        meta_db.upsert_source_state(domain, table, col, None, "unknown",
                                    str(e)[:200], source=source)
        return {"domain": domain, "source": source, "freshness": "unknown",
                "error": str(e)[:200]}

    last: Optional[date] = None
    if isinstance(raw, datetime):
        last = raw.date()
    elif isinstance(raw, date):
        last = raw
    elif isinstance(raw, str) and raw:
        try:
            last = datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            last = None

    freshness = classify(last)
    meta_db.upsert_source_state(domain, table, col,
                                last.isoformat() if last else None, freshness,
                                d.get("note", ""), source=source)
    return {"domain": domain, "source": source, "table": table, "date_col": col,
            "last_data_date": last.isoformat() if last else None,
            "freshness": freshness}


def probe_all() -> List[Dict[str, Any]]:
    return [probe_domain(d) for d in load_domains()]


def state() -> List[Dict[str, Any]]:
    """返回源域状态（不重新探测，供 API 快速返回）。"""
    rows = meta_db.list_source_state()
    today = date.today()
    for r in rows:
        last = r.get("last_data_date")
        d = None
        if last:
            try:
                d = datetime.strptime(last[:10], "%Y-%m-%d").date()
            except ValueError:
                d = None
        r["days_behind"] = (today - d).days if d else None
        r["stale"] = r.get("freshness") == "stale"
    return rows


def stale_domains() -> List[str]:
    return [r["domain"] for r in meta_db.list_source_state()
            if r.get("freshness") == "stale"]
