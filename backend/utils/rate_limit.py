"""轻量级进程内限流。

课程项目只有单后端进程，因此用内存滑动窗口即可挡住最简单的暴力登录、
批量注册、留资刷屏和公开聊天滥用。若未来改成多进程/多机部署，应替换为
Redis 等共享存储；本模块不假装自己是分布式限流。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse


_LOCK = threading.Lock()
_BUCKETS: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(request: Request, bucket: str, limit: int, window_seconds: int):
    """超出限制时返回 HTTP 429，否则返回 None。"""
    client = request.client.host if request.client else "unknown"
    key = bucket + ":" + client
    now = time.monotonic()
    cutoff = now - window_seconds
    with _LOCK:
        queue = _BUCKETS[key]
        while queue and queue[0] < cutoff:
            queue.popleft()
        if len(queue) >= limit:
            retry_after = max(1, int(window_seconds - (now - queue[0])))
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "code": 429,
                    "msg": "请求过于频繁，请稍后再试",
                    "data": None,
                },
            )
        queue.append(now)

        # 防止长时间运行时无界增长：只在桶数量较大时清理过期空桶。
        if len(_BUCKETS) > 5000:
            stale = [
                name for name, values in _BUCKETS.items()
                if not values or values[-1] < cutoff
            ]
            for name in stale:
                _BUCKETS.pop(name, None)
    return None
