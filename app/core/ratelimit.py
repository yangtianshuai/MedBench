# -*- coding: utf-8 -*-
"""数据库执行限流与并发保护。

★ 为什么必须有这一层：
    问数系统唯一的"物理杀伤力"就是它发出去的 SQL。HIS 只读库通常与生产同实例，
    一个「全院三年逐日明细」或几十个并发请求，就能把挂号/收费拖慢。
    医院对这类事故的处置通常是**直接停掉第三方系统的库权限** —— 项目到此结束。

所以限流不放在 HTTP 层（那只能挡住"人"，挡不住"一个请求跑 10 分钟"），
而是放在**执行器**：所有 SQL 都必须穿过这里。

两道闸：
    1. 并发闸：同时执行的 SQL 不超过 max_concurrent（默认 4）
    2. 速率闸：每分钟执行的 SQL 不超过 per_minute（默认 120）

超限的行为是**快速失败并回一句人话**，不是排队 —— 让用户在 3 秒内知道"系统忙"，
远好过等 60 秒然后超时（用户会连点，反而形成雪崩）。
"""

from __future__ import annotations

import collections
import threading
import time
from contextlib import contextmanager
from typing import Any, Deque, Iterator, Optional


class RateLimited(RuntimeError):
    """超过并发或速率上限。消息直接面向用户，必须是可读的中文。"""


class QueryLimiter:
    """进程内限流器（单实例部署够用；多副本时按副本数折算上限）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sem: Optional[threading.BoundedSemaphore] = None
        self._sem_size: int = 0
        self._hits: Deque[float] = collections.deque()
        self._stats = {"rejected_concurrency": 0, "rejected_rate": 0,
                       "executed": 0, "max_wait_ms": 0}

    # ── 配置读取（每次读，便于热改配置） ──
    @staticmethod
    def _limits() -> tuple:
        try:
            from app.config import settings
            conc = int(getattr(settings, "query_max_concurrent", 4) or 4)
            per_min = int(getattr(settings, "query_per_minute", 120) or 120)
            wait_ms = int(getattr(settings, "query_acquire_timeout_ms", 3000) or 3000)
        except Exception:  # noqa: BLE001 —— 配置异常时用保守默认值，不放开限制
            conc, per_min, wait_ms = 4, 120, 3000
        return max(1, conc), max(1, per_min), max(0, wait_ms)

    def _semaphore(self, size: int) -> threading.BoundedSemaphore:
        with self._lock:
            if self._sem is None or self._sem_size != size:
                self._sem = threading.BoundedSemaphore(size)
                self._sem_size = size
            return self._sem

    def _check_rate(self, per_minute: int) -> None:
        now = time.time()
        with self._lock:
            while self._hits and now - self._hits[0] > 60.0:
                self._hits.popleft()
            if len(self._hits) >= per_minute:
                self._stats["rejected_rate"] += 1
                raise RateLimited(
                    f"查询过于频繁（每分钟上限 {per_minute} 次），本次未执行。"
                    "请稍等片刻再试；如需批量取数请走导出或报表。")
            self._hits.append(now)

    @contextmanager
    def guard(self) -> Iterator[None]:
        conc, per_min, wait_ms = self._limits()
        self._check_rate(per_min)
        sem = self._semaphore(conc)
        started = time.time()
        if not sem.acquire(timeout=wait_ms / 1000.0):
            with self._lock:
                self._stats["rejected_concurrency"] += 1
            raise RateLimited(
                f"系统正忙（同时最多 {conc} 个查询在跑），本次未执行。"
                "请稍后重试；若持续出现，请联系信息科扩容或错峰查询。")
        waited_ms = int((time.time() - started) * 1000)
        try:
            with self._lock:
                self._stats["executed"] += 1
                self._stats["max_wait_ms"] = max(self._stats["max_wait_ms"], waited_ms)
            yield
        finally:
            sem.release()

    def snapshot(self) -> dict:
        conc, per_min, _ = self._limits()
        with self._lock:
            s = dict(self._stats)
            s.update({"max_concurrent": conc, "per_minute": per_min,
                      "in_last_minute": len(self._hits)})
        return s


limiter = QueryLimiter()
