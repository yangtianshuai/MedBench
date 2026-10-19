# -*- coding: utf-8 -*-
"""F4 报表与驾驶舱 + 导出（带水印与追溯）。"""

from __future__ import annotations

import csv
import io
import json
import time
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.core import authz, meta_db, permission, registry as reg

router = APIRouter(prefix="/api/reports", tags=["F4 报表与驾驶舱"])


@router.get("/dashboard")
def dashboard(request: Request, user_id: str = "guest", domain: str = None) -> Dict[str, Any]:
    """角色驾驶舱（简版）：指标目录 + 每个指标最近一次可信快照。

    MVP 不做法：实时轮询全部指标（会拖垮源库）。
    这里返回指标目录 + 最近快照，点击才真正查。

    ★ 快照按**本人**取（admin 取全局）：卡面上"最近一次"若是别人的那次查询，
      既泄露"某人问过这个指标"，也会让用户以为那是自己问过的数。
    """
    user = authz.principal(request, user_id)
    metrics = permission.filter_metrics(user, reg.list_metrics(domain=domain))
    snaps = {s["metric_code"]: s for s in meta_db.list_snapshots(
        limit=200, user_id=None if permission.is_admin(user) else user["user_id"])}

    cards = []
    for m in metrics:
        s = snaps.get(m["code"])
        cards.append({
            **m,
            "last_value_snapshot": ({
                "answer_id": s["answer_id"],
                "ts": s["ts"],
                "maturity": s["maturity"],
                "data_as_of": s["data_as_of"],
            } if s else None),
        })
    return {"user": {"user_id": user_id, "role": user["role"], "name": user["name"]},
            "registry_version": reg.registry_version(),
            "domains": reg.metric_domains(),
            "cards": cards}


@router.post("/export")
def export(payload: Dict[str, Any], request: Request) -> Dict[str, Any]:
    """导出（F4.4）：带水印，且必须留痕可追溯。

    ★ 身份取服务端解析结果（不是请求体里的 user_id）：水印要写"谁导出的"，
      这一栏如果由调用方填，水印本身就是可伪造的（docs/12 §6.4）。
    """
    user = authz.principal(request, payload.get("user_id"))
    if not user.get("can_export", False):
        raise HTTPException(status_code=403, detail="该角色无导出权限")

    answer_id = payload.get("answer_id")
    fmt = payload.get("fmt", "csv")

    # ★ 不接受前端提交的 rows（docs/13 §7 C5）：内容若由客户端给，
    #   「导出前先看水印」就是空话 —— 改一个数字照样导出，而审计记的是真快照 ID。
    #   导出内容一律由服务端按快照取，answer_id 是第一等公民。
    if not answer_id:
        raise HTTPException(status_code=400,
                            detail="导出必须指定 answer_id（内容由服务端按快照取，不接受前端提交的数据）")
    snap = meta_db.get_snapshot(answer_id)
    if not snap:
        raise HTTPException(status_code=404, detail="快照不存在")
    # 归属校验（12 §6.3）：导出的是**自己的**那一份结果；越权按 404 处理，不泄露存在性
    if snap.get("user_id") and snap["user_id"] != user["user_id"] \
            and not permission.is_admin(user):
        raise HTTPException(status_code=404, detail="快照不存在")
    rows: List[Dict[str, Any]] = json.loads(snap["result_json"]) if snap.get("result_json") else []

    # ★ 流水号先落到导出日志、再写进水印：顺序反过来会出现"文件里有号、日志里没有"
    #   的窗口期 —— 那正是反查时最尴尬的情况（无法区分"伪造"与"刚导完还没来得及记"）。
    serial = meta_db.log_export(answer_id, user["user_id"], fmt, "")

    watermark = ""
    if settings.export_watermark:
        watermark = (f"# 医院智能问数导出 | 操作人：{user['name']}({user['user_id']}) "
                     f"| 角色：{user['role']} | 时间：{time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"| 快照：{answer_id or '-'} | 导出流水号：{serial} | 禁止外传")

    buf = io.StringIO()
    if watermark:
        buf.write(watermark + "\n")
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    content = buf.getvalue()

    # 把最终水印（含流水号）补记到同一条导出记录上
    meta_db.set_export_watermark(serial, watermark)
    return {"fmt": fmt, "watermark": watermark, "serial": serial,
            "row_count": len(rows), "content": content}


@router.get("/exports/verify")
def verify_export(request: Request, serial: str) -> Dict[str, Any]:
    """按流水号反查一份导出（管理员）—— 用于核对流传到院外的文件。

    返回三态，必须区分清楚：
      - `format_ok=false`：编号本身就不合法（抄错 / 改过一位）→ **不是我们的文件**
      - `format_ok=true, found=false`：编号格式对，但日志里没有 → **伪造的编号**
      - `found=true`：确实是我们导出的，并给出操作人 / 快照 / 时间
    ★ 只有管理员能用：反查结果等于"这份敏感数据是谁导出去的"。
    """
    from app.core import export_trace
    authz.require_admin(request)
    ok, parts, why = export_trace.parse(serial)
    hits = meta_db.get_exports_by_serial((serial or "").strip().upper()) if ok else []
    return {"serial": serial, "format_ok": ok, "reason": why, "parsed": parts,
            "found": bool(hits), "records": hits,
            "conclusion": ("编号不合法（抄错或被改过一位）" if not ok else
                           ("日志里没有这个编号：**伪造的流水号**" if not hits else
                            "确实由本院系统导出，见 records"))}


@router.get("/exports")
def exports(request: Request, limit: int = 50) -> List[Dict[str, Any]]:
    """导出追溯：谁在什么时候导出了什么。

    ★ 这是一份"全院谁导过数据"的清单，等于反查敏感数据的流向 → 限管理员（12 §6.4）。
    """
    authz.require_admin(request)
    import sqlite3
    from app.config import settings as st
    conn = sqlite3.connect(st.meta_db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM export_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
