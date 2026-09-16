"""自研的百炼向量化适配器。

为什么不直接用 langchain-community 里的 DashScopeEmbeddings：
那个包已被 LangChain 官方弃用归档，不再接收安全补丁。自己实现只要几十行，
换来的是依赖干净、参数完全可控。

实现的是 LangChain 官方的 Embeddings 接口，所以能直接塞给 Chroma 用，
和用官方组件没有任何区别。

本实现相比"能跑就行"的版本，多做了三件事：
  1. 自动分批 —— 百炼接口单次最多接受 10 条文本，超了会直接报错
  2. 并发发送 —— 多个批次同时发，而不是排队一个个来
  3. 结果缓存 —— 同样的文本不重复调用接口，直接省下真金白银
"""

import asyncio
import hashlib
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import dashscope
from langchain_core.embeddings import Embeddings
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.config import settings

logger = logging.getLogger(__name__)

# 百炼向量化接口单次请求的文本条数上限。超过这个数接口会直接拒绝
DASHSCOPE_BATCH_LIMIT = 10


class EmbeddingAPIError(RuntimeError):
    """调用向量化接口失败。单独定义异常类型，方便上层识别并重试。"""


class DashScopeEmbeddings(Embeddings):
    """把文本转成向量。

    用法：
        emb = DashScopeEmbeddings()
        vecs = emb.embed_documents(["文本1", "文本2"])   # 批量，入库时用
        vec = emb.embed_query("用户的问题")              # 单条，检索时用
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        dimension: int | None = None,
        batch_size: int = DASHSCOPE_BATCH_LIMIT,
        max_concurrency: int = 4,
        cache_size: int = 5000,
        request_timeout: int = 60,
    ) -> None:
        # 单次请求的超时秒数。
        #
        # **这个值取多少是有讲究的，设小了反而更糟**：
        # 正常情况这个接口不到 1 秒就返回，但网络状况差的时候会慢到 20~30 秒。
        # 如果把超时设成 20 秒，那些"慢但能成功"的请求会被硬生生掐断，
        # 然后触发重试、再超时、再重试——本来 25 秒能拿到结果，
        # 变成 45 秒还在转圈。这是实测踩到的：一次提问的向量化被调用了 3 次。
        #
        # 所以要留出足够余量，让"慢"的请求能成功，只拦住真正"断了"的请求。
        # 60 秒相对 SDK 默认的 300 秒已经好了 5 倍，
        # 而且健康网络下永远不会触发到它。
        self.request_timeout = request_timeout
        self.model = model or settings.EMBEDDING_MODEL
        self.api_key = api_key or settings.DASHSCOPE_API_KEY
        self.dimension = dimension or settings.EMBEDDING_DIM

        if not self.api_key:
            raise ValueError(
                "没有配置 DASHSCOPE_API_KEY，请在 backend/.env 里填写你的百炼 API Key"
            )

        # 接口上限就是 10，配置写得再大也没用，这里直接夹住
        self.batch_size = min(batch_size, DASHSCOPE_BATCH_LIMIT)
        self.max_concurrency = max_concurrency

        # 结果缓存：文本指纹 → 向量。
        # 用 OrderedDict 实现简单的 LRU：超出容量就丢掉最久没用的那条。
        # 为什么值得做：文档重建索引、同一句话出现在多份资料里，都会重复请求，
        # 而这每一次都是要花钱的。
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._cache_size = cache_size
        self._cache_hits = 0
        self._api_calls = 0

    # ------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------
    @staticmethod
    def _fingerprint(text: str) -> str:
        """给文本算一个指纹，作为缓存键。

        用 SHA-256 而不是直接用文本本身当键：文本可能很长，
        直接当键会占用大量内存；而且缓存键里不该出现原文内容。
        """
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _cache_get(self, key: str) -> list[float] | None:
        if key in self._cache:
            # 命中后移到末尾，标记为"最近使用过"
            self._cache.move_to_end(key)
            self._cache_hits += 1
            return self._cache[key]
        return None

    def _cache_put(self, key: str, value: list[float]) -> None:
        self._cache[key] = value
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)  # 丢掉最久没用的

    @property
    def cache_stats(self) -> dict[str, int]:
        """缓存统计，可以放到统计看板上展示。"""
        total = self._cache_hits + self._api_calls
        return {
            "cache_hits": self._cache_hits,
            "api_calls": self._api_calls,
            "hit_rate_percent": round(self._cache_hits / total * 100, 1) if total else 0,
            "cache_entries": len(self._cache),
        }

    # ------------------------------------------------------------
    # 实际调用
    # ------------------------------------------------------------
    @retry(
        # 网络抖动、限流、服务端临时故障都值得重试
        retry=retry_if_exception_type(EmbeddingAPIError),
        # 指数退避：等 1s、2s、4s、8s 再试，最多试 4 次。
        # 为什么用指数退避：如果是被限流了，立刻重试只会继续被拒；
        # 越等越久才能给服务端喘息时间。
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(4),
        reraise=True,
    )
    def _call_api(self, texts: list[str]) -> list[list[float]]:
        """调用一次接口（这批文本不超过 10 条）。"""
        self._api_calls += 1

        try:
            resp = dashscope.TextEmbedding.call(
                model=self.model,
                input=texts,
                dimension=self.dimension,
                api_key=self.api_key,
                # **必须显式指定超时**。SDK 的默认值是 300 秒，
                # 而这个调用在正常网络下不到 1 秒。
                #
                # 不设的后果（实际踩到）：网络一出问题（比如 DNS 解析失败），
                # 每次尝试都会死死等满 5 分钟，再乘上下面 4 次重试，
                # 单次向量化最长能卡 20 分钟。从用户角度看就是"页面死了"，
                # 而且日志里什么都看不出来——因为请求确实还在"正常"进行中。
                #
                # 实测数据：一次检索因为网络抖动耗时 197 秒，
                # 分阶段打点显示时间全花在 retrieve 这一环。
                request_timeout=self.request_timeout,
            )
        except Exception as e:
            # dashscope 在超时、连接失败时会抛各种异常，统一转成自己的异常类型，
            # 这样 tenacity 才知道该重试
            raise EmbeddingAPIError(f"调用向量化接口出错：{e}") from e

        if resp.status_code != 200:
            raise EmbeddingAPIError(
                f"向量化接口返回错误 code={resp.status_code} message={resp.message}"
            )

        try:
            items = resp.output["embeddings"]
            # 必须按 text_index 排序还原顺序！
            # 接口返回的顺序不一定和传入顺序一致，直接按返回顺序用会导致
            # 「这段向量其实属于另一段文字」，检索结果会完全错乱。
            items = sorted(items, key=lambda x: x["text_index"])
            return [item["embedding"] for item in items]
        except (KeyError, TypeError) as e:
            raise EmbeddingAPIError(f"向量化接口返回的数据格式不符合预期：{e}") from e

    def _embed_batch_with_cache(self, texts: list[str]) -> list[list[float]]:
        """处理一批文本，命中的走缓存，没命中的才调接口。"""
        results: list[list[float] | None] = [None] * len(texts)
        pending_indices: list[int] = []
        pending_texts: list[str] = []

        for i, text in enumerate(texts):
            cached = self._cache_get(self._fingerprint(text))
            if cached is not None:
                results[i] = cached
            else:
                pending_indices.append(i)
                pending_texts.append(text)

        if pending_texts:
            vectors = self._call_api(pending_texts)
            for idx, text, vec in zip(pending_indices, pending_texts, vectors, strict=True):
                results[idx] = vec
                self._cache_put(self._fingerprint(text), vec)

        # 到这里 results 里不可能还有 None，用断言把这个前提钉住
        return [v for v in results if v is not None]

    # ------------------------------------------------------------
    # 批次调度
    # ------------------------------------------------------------
    def _to_batches(self, texts: list[str]) -> list[list[str]]:
        """把文本按接口上限切成若干批。"""
        return [
            texts[start : start + self.batch_size]
            for start in range(0, len(texts), self.batch_size)
        ]

    def _run_batches(self, batches: list[list[str]]) -> list[list[float]]:
        """并发跑多个批次，按原顺序拼回结果。

        为什么用线程池而不是一批批顺序跑：百炼接口单次只收 10 条文本，
        一份 100 块的中等文档就是 10 次网络往返。顺序跑要一次次等完整的
        往返时间，10 次叠加起来很可观；并发发出去后总耗时约等于最慢的那一批。

        并发数受 max_concurrency 限制，避免把接口打出限流。
        map() 会保持输入顺序，所以拼回来的结果和传入的文本一一对应。
        """
        if not batches:
            return []
        if len(batches) == 1:
            return self._embed_batch_with_cache(batches[0])

        output: list[list[float]] = []
        workers = min(self.max_concurrency, len(batches))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch_result in pool.map(self._embed_batch_with_cache, batches):
                output.extend(batch_result)
        return output

    # ------------------------------------------------------------
    # LangChain Embeddings 接口
    # ------------------------------------------------------------
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量把文本转成向量（同步版）。

        这是 LangChain Embeddings 接口要求实现的方法，向量库会调它。
        内部已经做了分批和并发，所以调用方不用关心批量上限的事。
        """
        if not texts:
            return []
        return self._run_batches(self._to_batches(texts))

    def embed_query(self, text: str) -> list[float]:
        """把单个查询转成向量（同步版）。检索时用。

        单独一个方法而不是复用 embed_documents，是因为某些向量模型对
        「查询」和「文档」会用不同的处理策略（比如加不同的前缀指令）。
        """
        return self._embed_batch_with_cache([text])[0]

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（异步版）。

        dashscope 的 SDK 只提供同步接口，如果直接在协程里调会把整个事件循环
        卡住——同一进程里其它用户的请求都得排队等这次向量化做完。
        所以丢进线程池执行，事件循环就能腾出来处理别的请求。
        """
        if not texts:
            return []
        return await asyncio.to_thread(self._run_batches, self._to_batches(texts))

    async def aembed_query(self, text: str) -> list[float]:
        """单条向量化（异步版）。"""
        result = await asyncio.to_thread(self._embed_batch_with_cache, [text])
        return result[0]


# 全局单例。缓存要在整个进程里共享才有意义，
# 每次新建实例缓存都是空的，等于白做。
_embeddings: DashScopeEmbeddings | None = None


def get_embeddings() -> DashScopeEmbeddings:
    """获取全局向量化实例（懒加载 + 单例）。"""
    global _embeddings
    if _embeddings is None:
        _embeddings = DashScopeEmbeddings()
    return _embeddings
