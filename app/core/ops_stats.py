# -*- coding: utf-8 -*-
"""运营榜单：热度 / 未命中 / 打回 / 指标使用。

★ 为什么单独做这个模块：
    系统里已经有 `miss_rate`、`answer_rate` 这类**比率**，但比率不能指导动作。
    "未命中率 12%" 没法开会；"这周被问 37 次但答不了的 5 个问题" 才能开会。
    语义层下一批补哪个指标，完全由这几个榜单决定 —— 没有它，本体迭代是拍脑袋。

★ 统计口径（都按"问题原话"聚合，归一化只做空白/标点/全角）：
    hot_questions      — 答得出来的问题，按次数排序（= 真实需求在哪）
    unmet_questions    — 答不出来的（out_of_scope/error/被打回），按次数排序（= 缺口在哪）
    hot_metrics        — 指标被调用次数（= 语义层资产的实际使用率）
    rejected_questions — 用户标了「数字不对/口径不对」的，按次数排序（= 信任风险在哪）

★ 刻意不做的事：
    - 不做"采纳率"推断（需要判断用户是否重问，误差大且会被误读成 KPI）
    - 不把院领导的问题原话外泄：本模块只输出聚合榜，调用方必须限管理员
"""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional

_PUNCT = re.compile(r"[\s，。？?！!、；;：:,.]+")


def normalize_question(q: Any) -> str:
    """用于聚合的问题归一化：只去空白与标点，不改写用词。

    ★ 不做同义词合并：把「门诊量」和「门诊人次」并成一个，会把口径差异
      从榜单里抹掉 —— 而那个差异往往正是要补的东西。
    """
    s = str(q or "").strip()
    s = _PUNCT.sub("", s)
    return s or "(空)"


def _within(ts: Optional[float], since: Optional[float]) -> bool:
    if since is None:
        return True
    return bool(ts) and float(ts) >= since


def rankings(audit_rows: Iterable[Dict[str, Any]],
             feedback_rows: Iterable[Dict[str, Any]],
             backlog_rows: Iterable[Dict[str, Any]],
             *, limit: int = 10, days: Optional[int] = None,
             now: Optional[float] = None) -> Dict[str, Any]:
    """把审计 / 反馈 / 回流三张表合成四个榜单（纯函数，便于单测）。"""
    now = time.time() if now is None else now
    since = None if days is None else now - days * 86400

    stat: Dict[str, Dict[str, Any]] = defaultdict(
        lambda: {"total": 0, "answered": 0, "miss": 0, "rejected": 0,
                 "last_ts": 0.0, "statuses": Counter()})

    for r in audit_rows:
        if not _within(r.get("ts"), since):
            continue
        q = normalize_question(r.get("question"))
        s = stat[q]
        s["total"] += 1
        st = str(r.get("status") or "")
        s["statuses"][st] += 1
        if st == "answered":
            s["answered"] += 1
        elif st in ("out_of_scope", "error"):
            s["miss"] += 1
        s["last_ts"] = max(s["last_ts"], float(r.get("ts") or 0))

    for r in feedback_rows:
        if not _within(r.get("ts"), since):
            continue
        kind = str(r.get("kind") or "")
        q = normalize_question(r.get("question"))
        if not q or q == "(空)":
            continue
        if kind in ("wrong_number", "wrong_caliber", "unclear"):
            stat[q]["rejected"] += 1
            stat[q]["last_ts"] = max(stat[q]["last_ts"], float(r.get("ts") or 0))

    # 回流队列里"还没答好"的问题（语义层缺口的直接证据）
    backlog_q: Counter = Counter()
    for r in backlog_rows:
        if str(r.get("status") or "pending") != "pending":
            continue
        if not _within(r.get("ts"), since):
            continue
        backlog_q[normalize_question(r.get("question"))] += 1

    hot: List[Dict[str, Any]] = []
    unmet: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for q, s in stat.items():
        row = {
            "question": q,
            "total": s["total"],
            "answered": s["answered"],
            "miss": s["miss"] + backlog_q.get(q, 0),
            "rejected": s["rejected"],
            "last_ts": s["last_ts"],
            "statuses": dict(s["statuses"]),
        }
        if s["answered"]:
            hot.append(row)
        if row["miss"]:
            unmet.append(row)
        if row["rejected"]:
            rejected.append(row)

    # 只回流、没有审计记录的问题也要出现在未命中榜（否则刚被打回的问题看不见）
    for q, n in backlog_q.items():
        if q not in stat:
            unmet.append({"question": q, "total": 0, "answered": 0, "miss": n,
                          "rejected": 0, "last_ts": 0.0, "statuses": {}})

    hot.sort(key=lambda r: (-r["answered"], -r["total"]))
    unmet.sort(key=lambda r: (-r["miss"], -r["rejected"], -r["total"]))
    rejected.sort(key=lambda r: (-r["rejected"], -r["total"]))

    metric_cnt: Counter = Counter()
    for r in audit_rows:
        if not _within(r.get("ts"), since):
            continue
        if str(r.get("status") or "") != "answered":
            continue
        mc = r.get("metric_code")
        if mc:
            metric_cnt[str(mc)] += 1

    return {
        "window_days": days,
        "hot_questions": hot[:limit],
        "unmet_questions": unmet[:limit],
        "rejected_questions": rejected[:limit],
        "hot_metrics": [{"metric_code": k, "count": v}
                        for k, v in metric_cnt.most_common(limit)],
        "totals": {
            "questions": sum(s["total"] for s in stat.values()),
            "answered": sum(s["answered"] for s in stat.values()),
            "unmet": len(unmet),
            "rejected": sum(s["rejected"] for s in stat.values()),
            "backlog_pending": sum(backlog_q.values()),
        },
    }


def selftest() -> bool:
    passed = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed
        if not cond:
            passed = False
        print(f"{'[OK]  ' if cond else '[FAIL]'} {name}" + (f"  {detail}" if detail else ""))

    now = 1_800_000_000.0
    audits = [
        {"question": "上个月出院人次", "status": "answered", "metric_code": "ip_discharge_cnt", "ts": now - 10},
        {"question": "上个月出院人次？", "status": "answered", "metric_code": "ip_discharge_cnt", "ts": now - 20},
        {"question": "床位使用率", "status": "out_of_scope", "metric_code": None, "ts": now - 30},
        {"question": "床位使用率", "status": "out_of_scope", "metric_code": None, "ts": now - 40},
        {"question": "患者满意度", "status": "answered", "metric_code": "op_visit_cnt", "ts": now - 50},
    ]
    fbs = [
        {"question": "上个月出院人次", "kind": "wrong_number", "ts": now - 5},
        {"question": "上个月出院人次", "kind": "correct", "ts": now - 6},
    ]
    backlog = [{"question": "床位使用率", "status": "pending", "ts": now - 30}]

    rep = rankings(audits, fbs, backlog, limit=5, days=30, now=now)

    hot = {r["question"]: r for r in rep["hot_questions"]}
    check("标点归一：两处问法合并为同一问题",
          "上个月出院人次" in hot and hot["上个月出院人次"]["answered"] == 2,
          f"hot={list(hot)}")
    unmet = {r["question"]: r for r in rep["unmet_questions"]}
    check("未命中榜：out_of_scope 2 次 + 回流 1 次 = 3",
          unmet.get("床位使用率", {}).get("miss") == 3, f"{unmet}")
    check("未命中榜按次数降序", rep["unmet_questions"][0]["question"] == "床位使用率")
    check("打回榜只统计非 correct 的反馈",
          rep["rejected_questions"][0]["question"] == "上个月出院人次"
          and rep["rejected_questions"][0]["rejected"] == 1)
    check("指标热度：ip_discharge_cnt 被用 2 次",
          rep["hot_metrics"][0] == {"metric_code": "ip_discharge_cnt", "count": 2})
    check("窗口外数据被排除",
          rankings([{"question": "很久以前", "status": "answered", "ts": now - 90 * 86400}],
                   [], [], days=30, now=now)["totals"]["questions"] == 0)
    check("不可答的问题不进热榜", "床位使用率" not in hot)
    return passed


if __name__ == "__main__":
    import sys
    ok = selftest()
    print("运营榜单自测: " + ("全部通过" if ok else "存在失败"))
    sys.exit(0 if ok else 1)
