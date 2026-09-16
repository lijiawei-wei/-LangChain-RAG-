"""文档处理流程测试：解析 → 分块 → 写向量库。

这里验证的是真实的处理管线，但**向量库被替换成了假的**——
所以不会真的调用百炼接口（不花钱、不依赖网络、跑得也快）。

测的是流程本身：状态有没有按顺序流转、分块数对不对、出错时有没有
正确标记为失败、重建索引会不会产生重复数据。
"""

import pytest

from app.models.document import Document, DocumentStatus
from app.rag import vectorstore
from app.services import document_service

API = "/api/v1"

SAMPLE = """# 星耀 X9 Pro 产品资料

## 一、电池与续航
内置 5000mAh 大容量电池，支持 67W 有线快充和 50W 无线快充。
实测有线快充 38 分钟可从 1% 充至 100%。

## 二、影像系统
后置三摄：5000 万像素主摄（索尼 IMX890，支持 OIS 光学防抖）、
800 万像素超广角、200 万像素微距。

## 三、保修政策
自购买之日起提供 12 个月整机保修，电池等易损耗部件保修 6 个月。
"""


def upload_doc(client, headers, kb_id, filename, content):
    return client.post(
        f"{API}/documents/upload",
        headers=headers,
        data={"kb_id": str(kb_id)},
        files={"files": (filename, content.encode("utf-8"), "text/markdown")},
    )


# ============================================================
# 正常流程
# ============================================================
class TestProcessSuccess:
    @pytest.mark.asyncio
    async def test_full_pipeline_success(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """完整跑一遍：解析 → 分块 → 写入向量库 → 标记完成。"""
        resp = upload_doc(client, admin_headers, sample_kb["id"], "产品.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        await real_process(doc_id)

        doc = patched_session.get(Document, doc_id)
        patched_session.refresh(doc)

        assert doc.status == DocumentStatus.READY
        assert doc.chunk_count > 0
        assert doc.indexed_at is not None
        assert doc.error_msg is None

        # 分块真的写进了向量库，而且数量对得上
        stored = fake_store.stored[sample_kb["id"]][doc_id]
        assert len(stored) == doc.chunk_count
        assert fake_store.add_calls == 1

    @pytest.mark.asyncio
    async def test_markdown_sections_become_separate_chunks(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """Markdown 应该按标题切成独立的块，每块只讲一个主题。"""
        resp = upload_doc(client, admin_headers, sample_kb["id"], "产品.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        await real_process(doc_id)

        chunks = fake_store.stored[sample_kb["id"]][doc_id]
        merged = "".join(chunks)

        # 三个章节的内容都要在
        for keyword in ["5000mAh", "IMX890", "12 个月整机保修"]:
            assert keyword in merged

        # 分块不该是整篇一大坨——至少要切成 3 块（对应三个章节）
        assert len(chunks) >= 3

    @pytest.mark.asyncio
    async def test_metadata_written_to_chunks(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """每个分块都要带上"来自哪份文档、哪个知识库"的信息。

        没有这些元数据，检索到内容后就没法告诉用户答案出自哪里，
        引用溯源功能就无从谈起。
        """
        resp = upload_doc(client, admin_headers, sample_kb["id"], "带来源.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        captured = {}

        async def capture(kb_id, docs, start_index=0):
            captured["docs"] = docs
            return len(docs)

        import app.services.document_service as ds

        ds.vectorstore.add_chunks = capture
        try:
            await real_process(doc_id)
        finally:
            ds.vectorstore.add_chunks = fake_store.add_chunks

        assert captured["docs"], "应该有分块被写入"
        meta = captured["docs"][0].metadata
        assert meta["doc_id"] == doc_id
        assert meta["kb_id"] == sample_kb["id"]
        assert meta["filename"] == "带来源.md"
        assert "chunk_index" in meta

    @pytest.mark.asyncio
    async def test_kb_stats_updated_after_processing(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """处理完成后，知识库的分块统计要更新。"""
        resp = upload_doc(client, admin_headers, sample_kb["id"], "统计.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        await real_process(doc_id)

        kb = client.get(f"{API}/knowledge-bases/{sample_kb['id']}", headers=admin_headers).json()["data"]
        assert kb["doc_count"] == 1
        assert kb["chunk_count"] > 0

    @pytest.mark.asyncio
    async def test_idempotent_when_already_ready(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """已经处理好的文档再跑一遍不该重复写入向量。"""
        resp = upload_doc(client, admin_headers, sample_kb["id"], "重复.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        await real_process(doc_id)
        assert fake_store.add_calls == 1

        await real_process(doc_id)
        # 第二次应该直接被跳过，不能再写一遍（否则向量库里会有两份重复内容）
        assert fake_store.add_calls == 1


# ============================================================
# 失败流程
# ============================================================
class TestProcessFailure:
    @pytest.mark.asyncio
    async def test_corrupted_file_marked_failed(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """文件内容损坏时，文档要标记为失败并记录原因。

        关键点：异常必须被吃掉，绝不能往外抛——后台任务的异常没人接管，
        会让整个进程留下隐患。
        """
        # 传一个后缀是 .pdf 但内容根本不是 PDF 的文件
        resp = client.post(
            f"{API}/documents/upload",
            headers=admin_headers,
            data={"kb_id": str(sample_kb["id"])},
            files={"files": ("假的.pdf", b"this is definitely not a pdf", "application/pdf")},
        )
        doc_id = resp.json()["data"][0]["document"]["id"]

        # 不该抛异常
        await real_process(doc_id)

        doc = patched_session.get(Document, doc_id)
        patched_session.refresh(doc)

        assert doc.status == DocumentStatus.FAILED
        assert doc.error_msg
        assert doc.chunk_count == 0

    @pytest.mark.asyncio
    async def test_empty_content_marked_failed(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """内容全是空白的文档要失败，不能入库一个空的分块。

        注意上传接口已经拦了「零字节」文件，但「只有空格换行」的文件
        能通过上传校验，要到解析阶段才发现——这是两道不同的防线。
        """
        resp = upload_doc(client, admin_headers, sample_kb["id"], "空白的.md", "   \n\n\t  \n")
        doc_id = resp.json()["data"][0]["document"]["id"]

        await real_process(doc_id)

        doc = patched_session.get(Document, doc_id)
        patched_session.refresh(doc)
        assert doc.status == DocumentStatus.FAILED

    @pytest.mark.asyncio
    async def test_vectorstore_error_marked_failed(
        self, client, admin_headers, sample_kb, real_process, patched_session, monkeypatch
    ):
        """向量库写入失败（比如接口欠费、网络断了）也要正确标记失败。"""
        async def broken_add(kb_id, docs, start_index=0):
            raise RuntimeError("向量化接口余额不足")

        import app.services.document_service as ds

        monkeypatch.setattr(ds.vectorstore, "add_chunks", broken_add)
        monkeypatch.setattr(ds.vectorstore, "delete_by_doc", lambda *a: _noop())

        resp = upload_doc(client, admin_headers, sample_kb["id"], "写失败.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        await real_process(doc_id)

        doc = patched_session.get(Document, doc_id)
        patched_session.refresh(doc)
        assert doc.status == DocumentStatus.FAILED
        assert "余额不足" in doc.error_msg


async def _noop(*args, **kwargs):
    return None


# ============================================================
# 重建索引
# ============================================================
class TestReindex:
    @pytest.mark.asyncio
    async def test_reindex_clears_old_vectors_first(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """重建索引要先清掉旧向量再写新的。

        如果只写不删，旧向量会一直留在库里，检索时同一个内容会被捞出来两次，
        既有重复的引用来源，又浪费存储。
        """
        resp = upload_doc(client, admin_headers, sample_kb["id"], "重建.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        # 第一次处理
        await real_process(doc_id)
        first_chunks = len(fake_store.stored[sample_kb["id"]][doc_id])
        assert fake_store.delete_calls == 1  # 写入前先清了一次

        # 重建
        document_service.prepare_reindex(patched_session, doc_id)
        await real_process(doc_id)

        second_chunks = len(fake_store.stored[sample_kb["id"]][doc_id])
        assert second_chunks == first_chunks, "重建后分块数应该一样，不能翻倍"
        assert fake_store.delete_calls == 2


# ============================================================
# 启动时的僵尸文档清理
# ============================================================
class TestResetStuckDocuments:
    def test_stuck_documents_marked_failed(self, db_session, monkeypatch):
        """服务重启时，卡在「处理中」的文档要标记为失败。

        否则用户会看到一个永远在转圈的进度条，不知道发生了什么。
        """
        from app.models.knowledge_base import KnowledgeBase
        from app.models.user import User
        from app.core.security import hash_password

        user = User(username="stuck_owner", password_hash=hash_password("x" * 10), role="admin")
        db_session.add(user)
        db_session.commit()

        kb = KnowledgeBase(name="僵尸测试库", created_by=user.id)
        db_session.add(kb)
        db_session.commit()

        # 造三个卡住的文档，分别卡在不同阶段
        docs = []
        for i, status_value in enumerate(
            [DocumentStatus.PARSING, DocumentStatus.CHUNKING, DocumentStatus.EMBEDDING]
        ):
            doc = Document(
                kb_id=kb.id,
                filename=f"卡住{i}.md",
                stored_path=f"/tmp/{i}.md",
                file_hash=f"hash{i}",
                file_type=".md",
                file_size=100,
                status=status_value,
            )
            db_session.add(doc)
            docs.append(doc)
        db_session.commit()

        import app.services.document_service as ds
        from tests.conftest import TestingSessionLocal

        monkeypatch.setattr(ds, "SessionLocal", TestingSessionLocal)

        count = document_service.reset_stuck_documents()
        assert count == 3

        for doc in docs:
            db_session.refresh(doc)
            assert doc.status == DocumentStatus.FAILED
            assert "服务重启" in doc.error_msg

    def test_ready_documents_not_touched(self, db_session, monkeypatch):
        """处理成功的文档不该被这个清理逻辑误伤。"""
        from app.core.security import hash_password
        from app.models.knowledge_base import KnowledgeBase
        from app.models.user import User

        user = User(username="ok_owner", password_hash=hash_password("x" * 10), role="admin")
        db_session.add(user)
        db_session.commit()

        kb = KnowledgeBase(name="正常库", created_by=user.id)
        db_session.add(kb)
        db_session.commit()

        doc = Document(
            kb_id=kb.id,
            filename="正常.md",
            stored_path="/tmp/ok.md",
            file_hash="ok_hash",
            file_type=".md",
            file_size=100,
            status=DocumentStatus.READY,
            chunk_count=5,
        )
        db_session.add(doc)
        db_session.commit()

        import app.services.document_service as ds
        from tests.conftest import TestingSessionLocal

        monkeypatch.setattr(ds, "SessionLocal", TestingSessionLocal)

        assert document_service.reset_stuck_documents() == 0

        db_session.refresh(doc)
        assert doc.status == DocumentStatus.READY
        assert doc.chunk_count == 5
