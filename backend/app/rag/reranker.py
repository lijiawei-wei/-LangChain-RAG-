"""基于百炼 gte-rerank-v2 的重排序组件。

**为什么需要重排**：召回和排序是两件事。

召回阶段（向量检索 / 关键词检索）为了不漏掉相关内容，会把网撒得很大
（我们取 20 条），代价是排在前面的不一定最相关。这叫"召回率高、准确率低"。

重排阶段用一个专门的**交叉编码器**模型，把「问题 + 候选文档」成对地
送进模型打分。它比向量检索准得多，原因是：
  - 向量检索是"双塔"结构：问题和文档**各自**编码成向量再算距离，
    两者在编码时互相看不到对方，信息有损失
  - 重排模型是"交叉"结构：问题和文档**一起**送进模型，
    能建模两者之间细粒度的交互关系

代价是慢——每对都要算一次，没法预先建索引。
所以标准做法是：召回阶段用快的方法捞 20 条，重排阶段用准的方法精选 5 条。

**关于自研**：`langchain-community` 里曾有个 `DashScopeRerank`，但那个包已被
官方弃用归档。这里自己实现，好处是能拿到原始分数做阈值过滤，
也能自己控制重试和降级策略。
"""

import logging

import dashscope
from langchain_core.callbacks import Callbacks
from langchain_core.documents import Document
from langchain_core.documents.compressor import BaseDocumentCompressor
from pydantic import Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# 单次请求能送多少篇文档给重排接口。接口文档写的上限是 500，
# 这里故意压到 100：一次送太多会明显变慢，而且我们最多也就召回几十条
MAX_DOCUMENTS_PER_CALL = 100


class RerankAPIError(RuntimeError):
    """调用重排接口失败。"""


class DashScopeReranker(BaseDocumentCompressor):
    """把召回结果按相关性重新排序。

    实现了 LangChain 的 BaseDocumentCompressor 接口，
    所以既能用在自研流水线里，也能直接接进标准的 LangChain 检索链。
    """

    model: str = Field(default_factory=lambda: settings.RERANK_MODEL)
    api_key: str = Field(default_factory=lambda: settings.DASHSCOPE_API_KEY)
    top_n: int = Field(default_factory=lambda: settings.RERANK_TOP_N)

    # 分数低于这个值的直接丢掉。
    # 默认 0 表示不过滤——不是因为这个值合理，而是因为**还没有依据**。
    # 实测 gte-rerank-v2 的分数分布很特别：相关内容的分数大约在 0.1~0.2，
    # 不相关的接近 0，跟常见的 0~1 分布完全不同。
    # 拍脑袋设 0.5 会把所有结果都过滤光。正确做法是用 M7 的评估模块
    # 跑真实问题集来标定，在那之前宁可不过滤。
    score_threshold: float = Field(default_factory=lambda: settings.RERANK_SCORE_THRESHOLD)

    # pydantic 模型默认不允许任意类型的字段，这里放开
    model_config = {"arbitrary_types_allowed": True}

    @retry(
        retry=retry_if_exception_type(RerankAPIError),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    def _call_api(self, query: str, documents: list[str], top_n: int):
        """调用一次重排接口。"""
        if not self.api_key:
            raise RerankAPIError("没有配置 DASHSCOPE_API_KEY")

        try:
            response = dashscope.TextReRank.call(
                model=self.model,
                query=query,
                documents=documents,
                top_n=top_n,
                return_documents=False,  # 文档内容我们自己有，不用接口回传，省流量
                api_key=self.api_key,
                # **必须显式设超时**，理由同 embeddings.py：
                # SDK 默认 300 秒，网络一抖动就会让整个问答卡住好几分钟，
                # 而且界面没有任何反馈，看起来就像服务挂了。
                # 取值同样要留余量——设太短会把"慢但能成功"的请求掐断，
                # 触发无谓的重试，反而更慢
                request_timeout=60,
            )
        except Exception as e:
            raise RerankAPIError(f"调用重排接口出错：{e}") from e

        if response.status_code != 200:
            raise RerankAPIError(
                f"重排接口返回错误 code={response.status_code} message={response.message}"
            )

        return response.output["results"]

    def rerank(
        self, query: str, documents: list[Document], top_n: int | None = None
    ) -> list[Document]:
        """对文档重新排序，返回得分最高的若干条。

        每个返回的文档的 metadata 里会多两个字段：
            rerank_score —— 重排模型给的相关性分数
            rerank_rank  —— 重排后的名次（从 1 开始）

        接口调用失败时会**降级返回原始顺序**，而不是抛异常。
        理由：重排只是精度优化，它挂了不该让整个问答功能不可用——
        用召回的结果照样能回答，只是没那么准。
        """
        if not documents:
            return []

        limit = top_n if top_n is not None else self.top_n

        # 文档太多就截断。重排接口对单次请求的文档数量有上限，
        # 而且送得越多越慢，收益递减
        candidates = documents[:MAX_DOCUMENTS_PER_CALL]
        texts = [doc.page_content for doc in candidates]

        try:
            results = self._call_api(query, texts, min(limit, len(texts)))
        except Exception as e:
            logger.warning("重排失败，降级使用原始召回顺序：%s", e)
            return documents[:limit]

        reranked: list[Document] = []
        for rank, item in enumerate(results, start=1):
            index = item["index"]
            score = float(item["relevance_score"])

            if score < self.score_threshold:
                continue

            if not (0 <= index < len(candidates)):
                # 接口返回了越界的索引（不该发生，但防御一下）
                logger.warning("重排接口返回了越界索引 %s，已跳过", index)
                continue

            doc = candidates[index]
            doc.metadata["rerank_score"] = round(score, 4)
            doc.metadata["rerank_rank"] = rank
            reranked.append(doc)

        # 全部被阈值过滤掉时，兜底返回原始结果的前几条。
        # 为什么不让它返回空：返回空会让整个问答变成"没有找到资料"，
        # 而实际上资料是有的、只是重排分数偏低。宁可给模型几条参考，
        # 让它在提示词约束下自己判断，也不要直接拒答。
        if not reranked:
            logger.info(
                "重排后所有结果都低于阈值 %.3f，降级返回原始召回结果", self.score_threshold
            )
            return documents[:limit]

        return reranked

    def compress_documents(
        self,
        documents,
        query: str,
        callbacks: Callbacks | None = None,
    ) -> list[Document]:
        """LangChain BaseDocumentCompressor 要求的接口方法。

        有了它，这个重排器可以直接接进标准的 LangChain 检索链，
        比如 ContextualCompressionRetriever。我们的自研流水线则直接调 rerank()。
        """
        return self.rerank(query, list(documents))


# 全局单例。重排器本身无状态，反复新建没有意义
_reranker: DashScopeReranker | None = None


def get_reranker() -> DashScopeReranker:
    """获取全局重排器。"""
    global _reranker
    if _reranker is None:
        _reranker = DashScopeReranker()
    return _reranker


def rerank(query: str, documents: list[Document], top_n: int | None = None) -> list[Document]:
    """便捷函数：对文档重新排序。"""
    return get_reranker().rerank(query, documents, top_n)
