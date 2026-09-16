"""进程内缓存。

**为什么需要缓存**：一次问答要经过查询改写、向量检索、关键词检索、重排、
大模型生成五道工序，其中三道要调云端接口，总共要花一两秒。

但实际使用中，很多问题是重复的——尤其是商品咨询场景，
「电池多大」「保修几年」这类问题会被不同用户反复问。
如果每次都完整跑一遍，既慢又费钱（每次都要花 token）。

缓存命中时直接返回上次的答案，耗时从一两秒降到几毫秒。

**为什么不用 Redis**：本机没有 Docker，起 Redis 要额外装东西。
进程内缓存在单实例部署下效果完全一样，而且更快（不用走网络）。
`CacheBackend` 这个抽象基类是为了将来能平滑换成 Redis——
那时候只需要新写一个实现类，业务代码一行都不用改。
"""

import hashlib
import logging
import threading
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)


class CacheBackend(ABC):
    """缓存后端的抽象接口。

    目前只有内存实现。要换成 Redis 时，继承这个类实现同样几个方法即可，
    调用方不需要任何改动。
    """

    @abstractmethod
    def get(self, key: str) -> Any | None: ...

    @abstractmethod
    def set(self, key: str, value: Any, ttl: int | None = None) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def stats(self) -> dict: ...


class InMemoryTTLCache(CacheBackend):
    """带过期时间的 LRU 内存缓存。

    两个淘汰机制：
      - **TTL（过期时间）**：超过指定秒数就作废。避免返回过期的信息
      - **LRU（最近最少使用）**：容量满了就丢掉最久没被访问的那条。
        防止缓存无限增长把内存吃满

    线程安全：用锁保护所有读写。因为缓存会被线程池里的任务访问
    （比如重排、向量化都是丢进线程池执行的），不加锁会出问题。
    """

    def __init__(self, maxsize: int = 1000, default_ttl: int = 3600) -> None:
        self._data: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._maxsize = maxsize
        self._default_ttl = default_ttl
        self._lock = threading.Lock()

        self._hits = 0
        self._misses = 0
        self._evictions = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None

            value, expire_at = entry
            if time.time() > expire_at:
                # 已过期，删掉并当作未命中
                del self._data[key]
                self._misses += 1
                return None

            # 命中后移到末尾，标记为"最近使用过"
            self._data.move_to_end(key)
            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        ttl = ttl if ttl is not None else self._default_ttl
        expire_at = time.time() + ttl

        with self._lock:
            self._data[key] = (value, expire_at)
            self._data.move_to_end(key)

            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)  # 丢掉最久没用的
                self._evictions += 1

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            logger.info("缓存已清空")

    def _purge_expired(self) -> int:
        """清理所有已过期的条目，返回清理数量。"""
        now = time.time()
        with self._lock:
            expired = [k for k, (_, exp) in self._data.items() if now > exp]
            for key in expired:
                del self._data[key]
            return len(expired)

    def stats(self) -> dict:
        total = self._hits + self._misses
        with self._lock:
            size = len(self._data)
        return {
            "backend": "memory",
            "size": size,
            "maxsize": self._maxsize,
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate_percent": round(self._hits / total * 100, 1) if total else 0.0,
        }


# ============================================================
# 全局缓存实例
# ============================================================
# 问答结果缓存。容量和存活时间都从配置读，方便不改代码就调整。
# 注意要延迟导入 settings：config.py 在导入链上更靠前，直接写在模块顶部
# 会有循环导入的风险，所以放进函数里按需取。
def _build_answer_cache() -> InMemoryTTLCache:
    from app.core.config import settings

    return InMemoryTTLCache(
        maxsize=settings.CACHE_MAX_SIZE,
        # 默认 1 小时。商品资料不会频繁变动，这个时长比较合适
        default_ttl=settings.CACHE_TTL_SECONDS,
    )


_answer_cache = _build_answer_cache()


def build_answer_key(question: str, kb_ids: list[int], config_fingerprint: str) -> str:
    """为一次问答生成缓存键。

    键里必须包含的东西：
      - 问题本身
      - 检索了哪些知识库（同一个问题在不同库里答案不同）
      - 检索配置的指纹（改了检索策略或提示词，旧缓存就该作废）

    为什么要把配置也算进去：开发阶段经常调参数（比如换分块大小、改提示词），
    如果不算进键里，改完参数测半天发现没变化——结果是命中了旧缓存。
    这个坑很浪费时间。
    """
    raw = f"{question}|{','.join(map(str, kb_ids))}|{config_fingerprint}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_answer(key: str) -> dict | None:
    """取缓存的问答结果。"""
    return _answer_cache.get(key)


def set_answer(key: str, value: dict, ttl: int | None = None) -> None:
    """写入问答结果缓存。"""
    _answer_cache.set(key, value, ttl)


def invalidate_answers() -> None:
    """让所有问答缓存失效。

    什么时候调用：知识库内容发生变化时（上传/删除/重建文档）。
    如果不失效，用户会一直拿到基于旧资料的答案，
    甚至可能引用到已经删掉的文档。
    """
    _answer_cache.clear()
    logger.info("知识库内容有变动，问答缓存已全部失效")


def get_cache_stats() -> dict:
    """缓存统计，供统计看板展示。"""
    return _answer_cache.stats()


def reset_cache_stats() -> None:
    """重置命中率统计。

    实现上是**换一个全新的缓存对象**，所以已缓存的数据也会一起没了。
    这个函数是给测试用的（避免上一个测试的命中次数污染下一个测试的断言），
    业务代码里不要调用它——想让数据失效请用 invalidate_answers()。
    """
    global _answer_cache
    _answer_cache = _build_answer_cache()
