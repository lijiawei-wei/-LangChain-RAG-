"""文档生命周期与并发保护测试。

这组测试守护的是一类**只在"处理中和删除同时发生"时才出现**的问题。
它们的共同特点是：平时完全看不出来，一旦触发就是数据错乱，而且极难排查。

背景（都是实际存在过的隐患）：

  1. **同一份文档被处理两次**：上传后立刻点「重建索引」，或者手快连点两次，
     两个后台任务同时跑同一份文档——重复调用云端接口（白花钱），
     两边交错地删旧向量、写新向量，最终内容说不准。

  2. **处理中把文档删了**：后台任务收尾时对已经不存在的行发 UPDATE，
     SQLAlchemy 抛 StaleDataError；更糟的是刚写进去的向量成了没人认领的孤儿。

  3. **处理中把知识库删了 —— 这个最阴险**：
     Chroma 的 collection 按知识库 ID 命名（kb_3），而 SQLite 的自增 ID **会被复用**。
     删掉知识库 3 之后新建一个，新库同样叫 kb_3，于是那个还在跑的任务
     写回去的旧向量就"属于"这个全新的空库了：
     界面上 0 份文档，一提问却能搜出已删除文件的内容。
"""

import pytest
from sqlalchemy import select

from app.models.document import Document, DocumentStatus
from app.services import document_service

API = "/api/v1"

SAMPLE = """# 星耀 X9 Pro 产品资料

## 一、电池与续航
内置 5000mAh 大容量电池，支持 67W 有线快充。
"""


def upload_doc(client, headers, kb_id, filename, content):
    return client.post(
        f"{API}/documents/upload",
        headers=headers,
        data={"kb_id": str(kb_id)},
        files={"files": (filename, content.encode("utf-8"), "text/markdown")},
    )


# ============================================================
# 重复提交保护
# ============================================================
class TestDuplicateProcessingGuard:
    @pytest.mark.asyncio
    async def test_second_submit_is_skipped(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """同一份文档被提交两次处理，第二次直接跳过。

        触发场景很常见：上传之后立刻点「重建索引」，这时状态还是"待处理"，
        判断"已处理成功就跳过"的逻辑拦不住它。
        """
        resp = upload_doc(client, admin_headers, sample_kb["id"], "资料.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        # 模拟"这份文档已经在处理队列里了"
        document_service._processing_ids.add(doc_id)
        try:
            await real_process(doc_id)
        finally:
            document_service._processing_ids.discard(doc_id)

        # 关键：一次向量都没写。写了就说明去重没生效，重复花钱了
        assert fake_store.add_calls == 0

    @pytest.mark.asyncio
    async def test_processing_ids_cleared_after_finish(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """处理结束后要把标记清掉，否则这份文档再也处理不了了。"""
        resp = upload_doc(client, admin_headers, sample_kb["id"], "资料.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        await real_process(doc_id)

        assert doc_id not in document_service._processing_ids

    @pytest.mark.asyncio
    async def test_processing_ids_cleared_even_on_failure(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session, monkeypatch
    ):
        """处理过程中抛异常时，标记也必须被清掉。

        用 try/finally 的原因就在这里：如果只在成功路径清，
        一次失败就会让这份文档**永久卡在"处理中"**，之后再也重建不了索引。
        """
        resp = upload_doc(client, admin_headers, sample_kb["id"], "资料.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        def boom(*args, **kwargs):
            raise RuntimeError("模拟处理过程中的意外错误")

        monkeypatch.setattr(document_service, "split_documents", boom)
        await real_process(doc_id)

        assert doc_id not in document_service._processing_ids


# ============================================================
# 删除时的拦截
# ============================================================
class TestDeleteWhileProcessing:
    def test_cannot_delete_document_while_processing(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """处理中的文档不允许删除，返回 409 并说清原因。"""
        resp = upload_doc(client, admin_headers, sample_kb["id"], "资料.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        document_service._processing_ids.add(doc_id)
        try:
            delete_resp = client.delete(
                f"{API}/documents/{doc_id}", headers=admin_headers
            )
        finally:
            document_service._processing_ids.discard(doc_id)

        assert delete_resp.status_code == 409
        assert "正在处理" in delete_resp.json()["message"]

        # 文档还在，不能因为删失败就被顺手删掉了
        assert patched_session.get(Document, doc_id) is not None

    def test_can_delete_after_processing_finishes(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """处理完之后就能正常删了（别把拦截做成永久禁止）。"""
        resp = upload_doc(client, admin_headers, sample_kb["id"], "资料.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        delete_resp = client.delete(f"{API}/documents/{doc_id}", headers=admin_headers)
        assert delete_resp.status_code == 200
        assert patched_session.get(Document, doc_id) is None


# ============================================================
# 处理过程中被删除
# ============================================================
class TestDeletedMidProcessing:
    @pytest.mark.asyncio
    async def test_document_deleted_midway_does_not_write_vectors(
        self, client, admin_headers, sample_kb, real_process, fake_store,
        patched_session, monkeypatch,
    ):
        """处理到一半文档被删掉，绝不能把向量写回去。

        这里用「在分块这一步顺手删掉数据库记录」来模拟真实的时序——
        删除是另一个请求发起的，和后台任务并发进行，没法用先后顺序去测。
        """
        resp = upload_doc(client, admin_headers, sample_kb["id"], "资料.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        original_split = document_service.split_documents

        def split_after_deleting(*args, **kwargs):
            # 模拟"就在解析分块的这几秒里，管理员把文档删了"
            patched_session.execute(select(Document).where(Document.id == doc_id))
            doc = patched_session.get(Document, doc_id)
            if doc is not None:
                patched_session.delete(doc)
                patched_session.commit()
            return original_split(*args, **kwargs)

        monkeypatch.setattr(document_service, "split_documents", split_after_deleting)
        await real_process(doc_id)

        # 关键断言：向量一条都没写进去。
        # 不拦的话，这些向量会永远留在 Chroma 里，
        # 检索时能搜出来，但界面上找不到对应的文档
        assert fake_store.add_calls == 0

    @pytest.mark.asyncio
    async def test_kb_deleted_midway_does_not_write_vectors(
        self, client, admin_headers, sample_kb, real_process, fake_store,
        patched_session, monkeypatch,
    ):
        """处理到一半整个知识库被删掉，同样不能写向量。

        这是最危险的一种：写回去的向量会"属于"之后新建的同 ID 知识库，
        造成"空库能搜出已删文档内容"的诡异现象。
        """
        from app.models.knowledge_base import KnowledgeBase

        resp = upload_doc(client, admin_headers, sample_kb["id"], "资料.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        original_split = document_service.split_documents

        def split_after_deleting_kb(*args, **kwargs):
            kb = patched_session.get(KnowledgeBase, sample_kb["id"])
            if kb is not None:
                patched_session.delete(kb)  # 级联删掉下面的文档
                patched_session.commit()
            return original_split(*args, **kwargs)

        monkeypatch.setattr(document_service, "split_documents", split_after_deleting_kb)
        await real_process(doc_id)

        assert fake_store.add_calls == 0

    @pytest.mark.asyncio
    async def test_kb_with_processing_docs_cannot_be_deleted(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """知识库下面有文档在处理时，整个库也不允许删。"""
        resp = upload_doc(client, admin_headers, sample_kb["id"], "资料.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        document_service._processing_ids.add(doc_id)
        try:
            delete_resp = client.delete(
                f"{API}/knowledge-bases/{sample_kb['id']}", headers=admin_headers
            )
        finally:
            document_service._processing_ids.discard(doc_id)

        assert delete_resp.status_code == 409
        assert "正在处理" in delete_resp.json()["message"]


# ============================================================
# 删除知识库时的文件清理
# ============================================================
class TestDeleteKnowledgeBaseFiles:
    def test_uploaded_files_removed_with_kb(
        self, client, admin_headers, sample_kb, real_process, fake_store, patched_session
    ):
        """删知识库要把它下面文档的原始文件一起删掉。

        不删的话上传目录会无限膨胀——实测跑下来，
        真实目录里堆了 227 个文件，数据库只引用其中 17 个。
        """
        from pathlib import Path

        resp = upload_doc(client, admin_headers, sample_kb["id"], "资料.md", SAMPLE)
        doc_id = resp.json()["data"][0]["document"]["id"]

        doc = patched_session.get(Document, doc_id)
        stored_path = Path(doc.stored_path)
        assert stored_path.exists(), "上传后原文件应该存在"

        delete_resp = client.delete(
            f"{API}/knowledge-bases/{sample_kb['id']}", headers=admin_headers
        )
        assert delete_resp.status_code == 200

        assert not stored_path.exists(), "删知识库之后原文件应该也被清掉"
