"""向量库封装层的测试。

测试用的是**真实的 Chroma**（跑在临时目录里，由 conftest 的
isolated_vectorstore 夹具保证不碰真实数据），但**向量化函数是假的**——
所以既能验证真实的存储和检索行为，又不会调用云端接口、不花钱、跑得快。
"""

import pytest
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.rag import vectorstore


class FakeEmbeddings(Embeddings):
    """假的向量化函数：把文字映射成确定性的向量。

    不调用任何外部接口。用文本的哈希值生成向量，
    所以相同文本一定得到相同向量，不同文本几乎一定不同——
    这足以验证「存入-取回-检索」这条链路是通的。
    """

    def __init__(self):
        self.call_count = 0

    def _vector(self, text: str) -> list[float]:
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # 展开成 64 维向量（真实模型是 1024 维，测试里小一点更快）
        return [((digest[i % 32] + i) % 100) / 100.0 for i in range(64)]

    def embed_documents(self, texts):
        self.call_count += 1
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


@pytest.fixture
def fake_embeddings(monkeypatch):
    """把向量库用的向量化函数换成假的。"""
    fake = FakeEmbeddings()
    import app.rag.vectorstore as vs

    monkeypatch.setattr(vs, "get_embeddings", lambda: fake)
    # get_store 会把 Chroma 对象缓存起来，缓存里可能已经绑定了真的向量函数，
    # 所以要把缓存清掉，保证测试里新建的是带假函数的实例
    vs._stores.clear()
    return fake


def make_docs(doc_id: int, kb_id: int, contents: list[str], filename="测试.md"):
    return [
        Document(
            page_content=text,
            metadata={
                "doc_id": doc_id,
                "kb_id": kb_id,
                "filename": filename,
                "chunk_index": i,
            },
        )
        for i, text in enumerate(contents)
    ]


# ============================================================
# 基本读写
# ============================================================
class TestBasicOperations:
    @pytest.mark.asyncio
    async def test_add_and_count(self, fake_embeddings):
        docs = make_docs(1, 1, ["电池容量 5000mAh", "支持 67W 快充", "屏幕 6.7 英寸"])
        written = await vectorstore.add_chunks(1, docs)

        assert written == 3
        assert vectorstore.count(1) == 3

    @pytest.mark.asyncio
    async def test_different_kbs_are_isolated(self, fake_embeddings):
        """不同知识库的向量要互不干扰——这是多知识库设计的基本要求。"""
        await vectorstore.add_chunks(1, make_docs(1, 1, ["知识库一的资料"]))
        await vectorstore.add_chunks(2, make_docs(2, 2, ["知识库二的资料"]))

        assert vectorstore.count(1) == 1
        assert vectorstore.count(2) == 1

    @pytest.mark.asyncio
    async def test_add_empty_list_is_noop(self, fake_embeddings):
        assert await vectorstore.add_chunks(1, []) == 0
        assert fake_embeddings.call_count == 0  # 没内容就不该调接口

    @pytest.mark.asyncio
    async def test_delete_by_doc_only_removes_that_doc(self, fake_embeddings):
        """删除一个文档的向量，不该把同知识库里其它文档的向量也删掉。"""
        await vectorstore.add_chunks(1, make_docs(1, 1, ["文档一的甲", "文档一的乙"]))
        await vectorstore.add_chunks(1, make_docs(2, 1, ["文档二的甲"]))

        assert vectorstore.count(1) == 3

        await vectorstore.delete_by_doc(1, 1)

        assert vectorstore.count(1) == 1

    @pytest.mark.asyncio
    async def test_drop_kb_removes_everything(self, fake_embeddings):
        await vectorstore.add_chunks(1, make_docs(1, 1, ["甲", "乙"]))
        assert vectorstore.count(1) == 2

        await vectorstore.drop_kb(1)

        assert vectorstore.count(1) == 0

    @pytest.mark.asyncio
    async def test_reindex_does_not_duplicate(self, fake_embeddings):
        """同一个文档重复写入不该产生重复向量。

        分块 ID 是稳定的（doc{文档ID}_c{序号}），所以重复写入会覆盖旧记录。
        如果 ID 里带了随机数，重建索引会让向量数量翻倍。
        """
        docs = make_docs(1, 1, ["甲", "乙", "丙"])

        await vectorstore.add_chunks(1, docs)
        await vectorstore.add_chunks(1, docs)

        assert vectorstore.count(1) == 3


# ============================================================
# 检索
# ============================================================
class TestSearch:
    @pytest.mark.asyncio
    async def test_search_returns_similarity_scores(self, fake_embeddings):
        """检索返回的应该是「相似度」（越大越相关），不是「距离」。"""
        await vectorstore.add_chunks(1, make_docs(1, 1, ["电池容量 5000mAh", "屏幕 6.7 英寸"]))

        results = vectorstore.search(1, "电池容量", k=2)

        assert len(results) == 2
        for _doc, score in results:
            # 余弦相似度换算后落在 [0, 1] 区间（1 表示完全一致）
            assert 0.0 <= score <= 1.0

    @pytest.mark.asyncio
    async def test_search_on_empty_kb_returns_empty(self, fake_embeddings):
        """还没传文档的知识库检索时应该返回空列表，不能报错。"""
        assert vectorstore.search(999, "随便问点什么", k=5) == []

    @pytest.mark.asyncio
    async def test_search_multi_skips_missing_kb(self, fake_embeddings):
        """多知识库检索时，某个库出问题不该让整个问答挂掉。"""
        await vectorstore.add_chunks(1, make_docs(1, 1, ["手机电池 5000mAh"]))

        # 其中一个知识库根本不存在，应该被跳过而不是抛异常
        results = await vectorstore.search_multi([1, 999], "电池", k=5)
        assert len(results) >= 1

    @pytest.mark.asyncio
    async def test_search_multi_empty_list(self, fake_embeddings):
        assert await vectorstore.search_multi([], "问题", k=5) == []


# ============================================================
# 集合失效自愈（回归测试）
# ============================================================
class TestCollectionSelfHealing:
    """验证「collection 被外部删除后，服务能自己恢复」。

    背景：get_store() 会缓存 Chroma 的 collection 句柄。如果这个 collection
    在别处被删了（另一个进程、手工清理、跑测试），缓存里的句柄就失效了，
    之后所有操作会一直报 NotFoundError 且不会自愈，非要重启服务。
    这个 bug 实际发生过，所以专门写测试锁住修复。
    """

    @pytest.mark.asyncio
    async def test_write_recovers_after_collection_dropped(self, fake_embeddings):
        await vectorstore.add_chunks(1, make_docs(1, 1, ["原始内容"]))
        assert vectorstore.count(1) == 1

        # 模拟「collection 被别的进程删掉了」：
        # 绕过我们自己的接口，直接从 Chroma 客户端删，这样 _stores 缓存
        # 里那个失效的句柄还留着——正是线上会出现的状态
        vectorstore.get_client().delete_collection("kb_1")

        # 缓存里还有旧句柄，此时直接操作会失败
        # 但我们的封装应该能捕获这个错误、清缓存、重建集合、完成写入
        written = await vectorstore.add_chunks(1, make_docs(1, 1, ["恢复后的新内容"]))

        assert written == 1
        assert vectorstore.count(1) == 1

    @pytest.mark.asyncio
    async def test_delete_recovers_after_collection_dropped(self, fake_embeddings):
        """删除操作同样要能自愈——虽然"删一个不存在的集合"结果都是没数据。"""
        await vectorstore.add_chunks(1, make_docs(1, 1, ["内容"]))
        vectorstore.get_client().delete_collection("kb_1")

        # 不该抛异常
        await vectorstore.delete_by_doc(1, 1)
        assert vectorstore.count(1) == 0

    @pytest.mark.asyncio
    async def test_search_recovers_after_collection_dropped(self, fake_embeddings):
        await vectorstore.add_chunks(1, make_docs(1, 1, ["电池 5000mAh"]))
        vectorstore.get_client().delete_collection("kb_1")

        # 集合没了，检索应该返回空而不是抛异常
        assert vectorstore.search(1, "电池", k=5) == []

    @pytest.mark.asyncio
    async def test_count_does_not_raise_on_missing_collection(self, fake_embeddings):
        """统计接口永远不该抛异常——它只是给界面显示个数字。"""
        assert vectorstore.count(12345) == 0


# ============================================================
# 分块读取
# ============================================================
class TestGetChunks:
    @pytest.mark.asyncio
    async def test_get_chunks_by_doc(self, fake_embeddings):
        await vectorstore.add_chunks(1, make_docs(1, 1, ["甲", "乙"], filename="a.md"))
        await vectorstore.add_chunks(1, make_docs(2, 1, ["丙"], filename="b.md"))

        result = vectorstore.get_chunks(1, doc_id=1)
        assert len(result["ids"]) == 2
        assert all("甲" in c or "乙" in c for c in result["documents"])

    @pytest.mark.asyncio
    async def test_get_all_chunks_returns_documents(self, fake_embeddings):
        await vectorstore.add_chunks(1, make_docs(1, 1, ["甲内容", "乙内容"]))

        docs = vectorstore.get_all_chunks(1)

        assert len(docs) == 2
        assert all(isinstance(d, Document) for d in docs)
        # 元数据要完整带回来——构建关键词索引时要靠它知道内容来源
        assert docs[0].metadata["doc_id"] == 1
