"""限流与并发控制。

**为什么需要**：
  1. 保护后端——某个用户写个脚本疯狂调问答接口，会把模型接口的额度刷光，
     也会让其它用户排不上队
  2. 保护钱包——每次问答都要花 token 的钱，没有限流就等于没有成本上限

采用**滑动窗口**算法而不是固定窗口：
固定窗口（"每分钟最多 60 次"按整分钟切分）有个漏洞——用户可以在
00:59 发 60 次、01:00 再发 60 次，两秒内实际发了 120 次。
滑动窗口看的是"最近 60 秒内发了多少次"，没有这个边界问题。
"""

import logging
import threading
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)


class SlidingWindowLimiter:
    """滑动窗口限流器。

    记录每个 key（通常用用户 ID）最近的请求时间戳，
    判断"最近 window_seconds 秒内的请求数"是否超过上限。
    """

    def __init__(self, max_requests: int, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds

        # key → 该 key 最近的请求时间戳队列
        self._records: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

        # 上次清理过期 key 的时间
        self._last_cleanup = time.time()

    def check(self, key: str) -> tuple[bool, int]:
        """检查是否允许这次请求。

        返回 (是否放行, 需要等待的秒数)。
        被限流时，调用方可以把等待秒数放进响应头告诉客户端什么时候能重试。
        """
        now = time.time()
        cutoff = now - self.window_seconds

        with self._lock:
            queue = self._records[key]

            # 把窗口外的记录丢掉（这些请求已经"过期"了，不再计入）
            while queue and queue[0] < cutoff:
                queue.popleft()

            if len(queue) >= self.max_requests:
                # 被限流。等到队首那条记录滑出窗口，就有名额了
                wait = int(queue[0] - cutoff) + 1
                return False, max(wait, 1)

            queue.append(now)

        self._maybe_cleanup(now)
        return True, 0

    def _maybe_cleanup(self, now: float) -> None:
        """定期清理不再活跃的 key。

        为什么需要：每个访问过的用户都会在字典里留下一条记录，
        如果用户量很大，字典会一直膨胀。这里每隔一段时间
        把已经没有记录的 key 删掉，释放内存。
        """
        if now - self._last_cleanup < self.window_seconds * 5:
            return

        cutoff = now - self.window_seconds
        with self._lock:
            stale = [k for k, q in self._records.items() if not q or q[-1] < cutoff]
            for key in stale:
                del self._records[key]

        self._last_cleanup = now

        if stale:
            logger.debug("限流器清理了 %d 个不活跃的 key", len(stale))

    def reset(self, key: str | None = None) -> None:
        """重置限流计数（测试用，或管理员手动解封）。"""
        with self._lock:
            if key is None:
                self._records.clear()
            else:
                self._records.pop(key, None)

    def stats(self) -> dict:
        with self._lock:
            return {
                "tracked_keys": len(self._records),
                "max_requests": self.max_requests,
                "window_seconds": self.window_seconds,
            }


class ConcurrencyLimiter:
    """并发数限制器。

    和"每分钟多少次"的限流是两回事：这个管的是**同时**有几个请求在跑。

    为什么需要单独管并发：问答接口一次要跑一两秒，用户如果开十个标签页
    同时提问，就有十个请求同时占用模型接口连接和线程池。
    限制并发能保证资源不被少数用户占满，其他人还能正常使用。
    """

    def __init__(self, max_concurrent: int) -> None:
        self.max_concurrent = max_concurrent
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    def acquire(self, key: str) -> bool:
        """尝试占用一个名额。返回是否成功。"""
        with self._lock:
            if self._counts[key] >= self.max_concurrent:
                return False
            self._counts[key] += 1
            return True

    def release(self, key: str) -> None:
        """释放一个名额。**必须在 finally 里调用**，否则名额会漏掉。"""
        with self._lock:
            if self._counts.get(key, 0) > 0:
                self._counts[key] -= 1
                if self._counts[key] == 0:
                    del self._counts[key]

    def current(self, key: str) -> int:
        with self._lock:
            return self._counts.get(key, 0)

    def stats(self) -> dict:
        with self._lock:
            return {
                "active_users": len(self._counts),
                "total_in_flight": sum(self._counts.values()),
                "max_concurrent_per_user": self.max_concurrent,
            }


# ============================================================
# 全局实例
# ============================================================
from app.core.config import settings  # noqa: E402  放在这里避免循环导入

# 通用接口限流：每个用户每分钟最多 N 次请求
api_limiter = SlidingWindowLimiter(
    max_requests=settings.RATE_LIMIT_PER_MINUTE, window_seconds=60
)

# 问答接口单独限流，比通用接口更严格——因为它每次都要花钱调模型
chat_limiter = SlidingWindowLimiter(
    max_requests=max(settings.RATE_LIMIT_PER_MINUTE // 2, 10), window_seconds=60
)

# 单用户并发问答数上限
chat_concurrency = ConcurrencyLimiter(settings.CHAT_CONCURRENCY_PER_USER)


def get_rate_limit_stats() -> dict:
    """限流状态汇总，供统计看板展示。"""
    return {
        "api": api_limiter.stats(),
        "chat": chat_limiter.stats(),
        "concurrency": chat_concurrency.stats(),
    }
