"""中文 BM25 关键词检索器。

**为什么需要它**：向量检索擅长理解语义，但对精确的字符匹配不敏感。

举个例子，用户问「IMX890 这个传感器怎么样」：
  - 向量检索可能返回一堆讲"拍照"的段落，但恰好漏掉写着 IMX890 的那段
  - 关键词检索则能精准命中 —— 因为"IMX890"这个词就在那段文字里

反过来，用户问「电池能用多久」（资料里写的是"续航时间"）：
  - 关键词检索匹配不上（字面不一样）
  - 向量检索能理解它们是一个意思

所以两者是互补的，混着用（混合检索）效果最好。这也是 Dify、Coze 这些
平台默认的检索方案。

**关于中文分词**：BM25 算法本身是按"词"来算的。英文天然用空格分词，
中文句子中间没有空格，如果直接按空格切，整句话会变成一个"词"，
BM25 就完全失效了。所以必须先用 jieba 分词。
"""

import logging
import threading

import jieba
from langchain_core.documents import Document
from rank_bm25 import BM25Okapi

from app.rag import vectorstore

logger = logging.getLogger(__name__)

# 关掉 jieba 的启动日志。
# 它首次运行时会往控制台打一大段"Building prefix dict..."，
# 混在业务日志里很干扰排查
jieba.setLogLevel(logging.WARNING)


# 中文里的虚词和疑问词。它们在每个分块里都可能出现，不携带检索信息，
# 留着反而会干扰 BM25 的打分。
#
# 为什么必须过滤（实测踩过的坑）：
#   用户问「电池容量是多少？」，如果不过滤，BM25 会匹配到"是""多少"这两个词，
#   于是**把包含"价格是多少"的常见问题段落排到了第一位**，
#   而真正讲电池的那一段得分为 0 —— 完全没能召回。
#   这两个词的 IDF 还不低（"多少"在这批文档里算罕见词），
#   所以它们产生的是一个高分噪声。
STOPWORDS = frozenset(
    """
    的 了 是 在 有 和 与 或 及 就 都 也 还 又 但 而 并 对 为 以 之 其 此
    吗 呢 吧 啊 呀 哦 嗯 么
    我 你 他 她 它 我们 你们 他们 她们 它们
    这 那 这个 那个 这些 那些 这样 那样
    什么 怎么 怎样 怎么样 如何 为何 为什么 多少 哪些 哪个 哪里 是否 能否
    可以 能否 需要 应该 可能 会 能 要 想 请 帮 帮 我查 一下 关于 请问
    一个 一些 一种 以及 还有 或者 但是 因为 所以 如果 虽然 然后 就是
    介绍 说明 告诉 知道 了解 看看 说说
    """.split()
)

# 单字停用词单独放一份，用于快速判断
_SINGLE_CHAR_STOPWORDS = frozenset(w for w in STOPWORDS if len(w) == 1)


def tokenize(text: str) -> list[str]:
    """把中文文本切成适合检索的词序列。

    相比朴素做法的三处关键处理：

    1. **用搜索引擎模式分词（lcut_for_search），而不是精确模式**
       精确模式会把「电池容量」当成一个完整的词，于是它匹配不到资料里
       写的「大容量电池」——词序不同就完全失配。
       搜索引擎模式会把长词**同时切出它的组成部分**：
           lcut("电池容量")        → ['电池容量']
           lcut_for_search(同)     → ['电池', '容量', '电池容量']
       这样只要资料里出现「电池」或「容量」任一个，就能命中。
       这是 jieba 专门为检索场景提供的模式，用错模式是中文检索最常见的问题。

    2. **过滤停用词**
       见上面 STOPWORDS 的说明，不过滤会让「是多少」这类词主导打分。

    3. **统一转小写**
       让英文型号 "imx890" 和 "IMX890" 能匹配上。
    """
    words = jieba.lcut_for_search(text.lower())

    result = []
    for word in words:
        word = word.strip()
        if not word:
            continue
        # 单个字符只有在是英文字母或数字时才保留（比如型号里的 "X"），
        # 单个汉字（如"是""的"）没有检索价值
        if len(word) == 1 and not word.isalnum():
            continue
        if word in STOPWORDS:
            continue
        result.append(word)

    return result


class BM25Index:
    """某个知识库的关键词索引。

    索引建好之后常驻内存，检索时不再访问磁盘——这是它比向量检索快得多的原因
    （向量检索要调接口算查询向量，再在库里做相似度搜索；关键词检索是纯内存计算，
    通常在几毫秒内完成）。
    """

    def __init__(self, kb_id: int, docs: list[Document]) -> None:
        self.kb_id = kb_id
        self.docs = docs
        self.corpus_size = len(docs)

        # 预先分词。这是建索引里最耗时的一步，放在初始化时做一次，
        # 后续每次检索就只给查询分词了
        self.tokenized_corpus = [tokenize(d.page_content) for d in docs]

        self.bm25 = BM25Okapi(self.tokenized_corpus) if self.tokenized_corpus else None

        logger.info("知识库 %s 的关键词索引已就绪（%d 个分块）", kb_id, self.corpus_size)

    def search(self, query: str, k: int = 5) -> list[tuple[Document, float]]:
        """检索最相关的 k 个分块。

        返回 (分块, 分数)。注意 BM25 的分数**没有固定取值范围**——
        它可能是 0 到 20 之间的任意数，大小只在本批结果内有相对意义。
        所以后面跟向量检索融合时用的是 RRF（只看排名不看分数），
        而不是加权求和。
        """
        if self.bm25 is None or self.corpus_size == 0:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scores = self.bm25.get_scores(query_tokens)

        # 取分数最高的 k 个。
        # 用 heapq.nlargest 而不是全排序：分块多的时候（几万个）全排序要浪费不少时间，
        # 而这里只需要前 k 个
        import heapq

        top_indices = heapq.nlargest(k, range(len(scores)), key=lambda i: scores[i])

        results: list[tuple[Document, float]] = []
        for idx in top_indices:
            score = float(scores[idx])
            # 分数为 0 表示完全没有关键词命中，这种结果没有意义，直接丢掉
            if score > 0:
                results.append((self.docs[idx], score))

        return results


# ============================================================
# 索引缓存
# ============================================================
# kb_id → BM25Index
_index_cache: dict[int, BM25Index] = {}

# 读写锁。用普通的 threading.Lock 而不是 asyncio.Lock，是因为
# 建索引是个 CPU 密集操作，会被丢到线程池里跑（见 search 函数），
# 那时候不在事件循环里，用不了 asyncio 的锁
_index_lock = threading.Lock()


def get_index(kb_id: int) -> BM25Index:
    """获取知识库的关键词索引（惰性构建 + 缓存）。

    为什么用"惰性构建"而不是在文档入库时重建：
    如果一份文档传进来就重建一次，批量上传 10 个文件就要重建 10 次索引，
    每次都把整个知识库的文本重新分词一遍，非常浪费。
    改成"第一次检索时才建"，批量上传期间一次都不建，传完第一次提问才建一次。
    """
    with _index_lock:
        cached = _index_cache.get(kb_id)
        if cached is not None:
            return cached

    # 建索引比较慢（要读全库文本 + 分词），放到锁外面做，
    # 免得把其它知识库的检索也一起卡住
    docs = vectorstore.get_all_chunks(kb_id)
    index = BM25Index(kb_id, docs)

    with _index_lock:
        _index_cache[kb_id] = index
    return index


def invalidate(kb_id: int) -> None:
    """让某个知识库的索引失效。

    文档增删改之后必须调用，否则检索到的会是已经不存在的内容，
    或者漏掉刚上传的新资料。
    下次检索时会重新构建。
    """
    with _index_lock:
        if _index_cache.pop(kb_id, None) is not None:
            logger.info("知识库 %s 的关键词索引已失效，将在下次检索时重建", kb_id)


def invalidate_all() -> None:
    """让所有知识库的索引失效。"""
    with _index_lock:
        _index_cache.clear()
        logger.info("全部关键词索引已失效")


def search(kb_id: int, query: str, k: int = 5) -> list[tuple[Document, float]]:
    """在单个知识库里做关键词检索。"""
    return get_index(kb_id).search(query, k)


def get_cache_stats() -> dict:
    """索引缓存状态，供统计看板展示。"""
    with _index_lock:
        return {
            "cached_kbs": len(_index_cache),
            "total_chunks": sum(idx.corpus_size for idx in _index_cache.values()),
        }
