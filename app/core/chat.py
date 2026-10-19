# -*- coding: utf-8 -*-
"""会话与消息（F1.9）—— 客户端「以对话为核心」的地基。

为什么单独一个模块而不是写在路由里：
    `/api/ask`（问一句）与 `/api/analysis/*`（下钻/排名/趋势/对比）都要往同一个会话里
    追加消息。渲染载荷的裁剪规则（行数上限）与会话归属校验必须是**一份实现**，
    否则前端从两条路径拿到的消息形态会不一致，回放就会有的能渲染有的不能。

三条硬约束：
  1. **归属校验**：只能读到自己会话（12 §6.3）。别人的会话一律 404，不泄露"存在"。
  2. **载荷只存渲染所需**：rows 截断到 `ROWS_CAP` 行并记录 `row_count`；
     完整结果在 `snapshot.result_json` 里，回放以快照为准。
  3. **失败也要落库**：拒绝/超范围/故障同样是这一轮对话的内容，
     必须留在会话里（否则用户回看历史时会以为"我没问过"）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from app.core import meta_db

# 渲染载荷里保留的最大数据行数。
# 为什么不存全量：会话消息是**回放**用的，一次 5000 行的结果塞进 SQLite 会让
# 会话列表/回放接口变成慢查询；真有需要看全量，走快照与导出口子。
ROWS_CAP = 200

# 渲染一个答案卡片真正需要的字段（与 app/web 的 render() 一一对应）
_PAYLOAD_KEYS = (
    "answer_id", "status", "question", "answer", "metric_code", "intent",
    "dsl", "sql", "caliber", "clarify", "warnings", "trace", "trust",
    "latency_ms", "rollup", "backlogged", "user",
)


def ensure_session(user_id: str, session_id: Optional[str] = None,
                   title: str = "") -> str:
    """拿到一个可用的会话 ID。

    - 传了 session_id 且**属于该用户** → 复用；
    - 传了但不属于该用户（或不存在/已删除）→ **新建一个**，并保留原 session_id 不可见：
      直接复用会造成越权写入，直接报错会泄露"这个会话存在"。新建是唯一不撒谎的选择。
    """
    if session_id:
        s = meta_db.get_session(session_id, user_id=user_id)
        if s:
            return session_id
    return meta_db.create_session(user_id, title=title)


def payload_summary(out: Dict[str, Any]) -> Dict[str, Any]:
    """把 `/api/ask` 的完整响应裁成可回放的渲染载荷。"""
    p: Dict[str, Any] = {k: out.get(k) for k in _PAYLOAD_KEYS if k in out}
    rows = out.get("rows")
    if isinstance(rows, list):
        p["rows"] = rows[:ROWS_CAP]
        p["row_count"] = len(rows)
        p["rows_truncated"] = len(rows) > ROWS_CAP
    elif rows is None:
        p["rows"] = None
    return p


def log_turn(session_id: str, user_id: str, question: str,
             out: Optional[Dict[str, Any]] = None,
             status: Optional[str] = None,
             answer_id: Optional[str] = None) -> int:
    """一轮 = 一条 user 消息 + 一条 assistant 消息。返回 assistant 消息 id。

    out 为空表示"这一轮根本没走到出数"（例如异常），仍然要留下 user 消息 ——
    否则会话历史看起来像消息丢了。
    """
    meta_db.append_message(session_id, "user", user_id, question=question)
    if out is None:
        return meta_db.append_message(session_id, "assistant", user_id, question=question,
                                      status=status or "error", answer_id=answer_id)
    return meta_db.append_message(
        session_id, "assistant", user_id, question=out.get("question") or question,
        status=out.get("status"), answer_id=out.get("answer_id"),
        metric_code=out.get("metric_code"), payload=payload_summary(out))


def load_thread(session_id: str, user_id: str) -> Optional[Dict[str, Any]]:
    """会话 + 消息（归属校验失败返回 None，由路由层转 404）。"""
    s = meta_db.get_session(session_id, user_id=user_id)
    if not s:
        return None
    msgs = meta_db.list_messages(session_id)
    s["messages"] = msgs
    s["message_count"] = len(msgs)
    return s
