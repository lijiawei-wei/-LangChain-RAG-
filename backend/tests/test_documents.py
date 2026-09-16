"""文档管理接口测试。

这些测试把「后台处理」换成了空操作（见 conftest 的 real_process），
所以只验证接口层的逻辑：参数校验、幂等去重、权限、分页、删除等。
真实的解析+向量化流程在 test_document_processing.py 里验证。
"""

import pytest

API = "/api/v1"

SAMPLE_CONTENT = """# 星耀 X9 Pro 产品资料

## 电池与续航
内置 5000mAh 大容量电池，支持 67W 有线快充。
"""


def upload(client, headers, kb_id, filename, content: str | bytes, content_type="text/markdown"):
    """上传一个文件的辅助函数。

    接受 str 或 bytes：传字符串时自动按 UTF-8 编码。
    这样调用处可以直接写中文内容，不用每处都 .encode()，
    （也顺便避开了 Python 不允许 b"中文" 这种写法的限制）。
    """
    if isinstance(content, str):
        content = content.encode("utf-8")

    return client.post(
        f"{API}/documents/upload",
        headers=headers,
        data={"kb_id": str(kb_id)},
        files={"files": (filename, content, content_type)},
    )


# ============================================================
# 权限
# ============================================================
class TestDocumentPermissions:
    def test_anonymous_cannot_upload(self, client, sample_kb):
        resp = upload(client, {}, sample_kb["id"], "a.md", SAMPLE_CONTENT.encode())
        assert resp.status_code == 401

    def test_normal_user_cannot_upload(self, client, auth_headers, sample_kb, real_process):
        resp = upload(client, auth_headers, sample_kb["id"], "a.md", SAMPLE_CONTENT.encode())
        assert resp.status_code == 403

    def test_normal_user_cannot_list(self, client, auth_headers):
        assert client.get(f"{API}/documents", headers=auth_headers).status_code == 403

    def test_normal_user_cannot_delete(self, client, auth_headers):
        assert client.delete(f"{API}/documents/1", headers=auth_headers).status_code == 403

    def test_normal_user_cannot_reindex(self, client, auth_headers):
        assert client.post(f"{API}/documents/1/reindex", headers=auth_headers).status_code == 403

    def test_normal_user_cannot_view_chunks(self, client, auth_headers):
        assert client.get(f"{API}/documents/1/chunks", headers=auth_headers).status_code == 403


# ============================================================
# 上传
# ============================================================
class TestUploadDocument:
    def test_upload_markdown(self, client, admin_headers, sample_kb, real_process):
        resp = upload(client, admin_headers, sample_kb["id"], "商品.md", SAMPLE_CONTENT.encode())
        assert resp.status_code == 201

        result = resp.json()["data"][0]
        assert result["success"] is True
        assert result["result"] == "created"
        assert result["document"]["filename"] == "商品.md"
        assert result["document"]["file_type"] == ".md"
        assert result["document"]["status"] == "pending"

    def test_upload_multiple_files(self, client, admin_headers, sample_kb, real_process):
        """一次可以传多个文件，每个文件单独返回结果。"""
        resp = client.post(
            f"{API}/documents/upload",
            headers=admin_headers,
            data={"kb_id": str(sample_kb["id"])},
            files=[
                ("files", ("甲.md", "# 甲\n\n内容甲".encode(), "text/markdown")),
                ("files", ("乙.md", "# 乙\n\n内容乙".encode(), "text/markdown")),
            ],
        )
        results = resp.json()["data"]
        assert len(results) == 2
        assert all(r["success"] for r in results)

    def test_duplicate_content_detected(self, client, admin_headers, sample_kb, real_process):
        """同样内容的文件第二次上传要被识别出来，不重复占用空间。

        注意是按**内容**判断而不是文件名：同一份资料改个名字再传很常见。
        """
        upload(client, admin_headers, sample_kb["id"], "原名.md", SAMPLE_CONTENT.encode())
        resp = upload(client, admin_headers, sample_kb["id"], "改了个名字.md", SAMPLE_CONTENT.encode())

        result = resp.json()["data"][0]
        assert result["result"] == "duplicated"
        assert "完全相同" in result["message"]

        # 库里只有一条记录
        listing = client.get(f"{API}/documents?kb_id={sample_kb['id']}", headers=admin_headers)
        assert listing.json()["data"]["total"] == 1

    def test_same_name_different_content_allowed(self, client, admin_headers, sample_kb, real_process):
        """文件名一样但内容不同，应该都能入库（内容才是判断依据）。"""
        upload(client, admin_headers, sample_kb["id"], "同名.md", "# 第一版\n\n内容一")
        resp = upload(client, admin_headers, sample_kb["id"], "同名.md", "# 第二版\n\n内容二")

        assert resp.json()["data"][0]["result"] == "created"

    def test_partial_failure_does_not_break_batch(self, client, admin_headers, sample_kb, real_process):
        """一条失败不该让整批都失败——这样用户不用把成功的文件再传一遍。"""
        resp = client.post(
            f"{API}/documents/upload",
            headers=admin_headers,
            data={"kb_id": str(sample_kb["id"])},
            files=[
                ("files", ("正常的.md", "# 正常\n\n内容", "text/markdown")),
                ("files", ("不支持的.png", b"fake image", "image/png")),
            ],
        )
        results = resp.json()["data"]
        assert results[0]["success"] is True
        assert results[1]["success"] is False
        assert "不支持" in results[1]["message"]

    @pytest.mark.parametrize(
        "filename,content",
        [
            ("病毒.exe", b"MZ\x90\x00"),
            ("图片.png", b"\x89PNG"),
            ("压缩包.zip", b"PK\x03\x04"),
            ("老格式.doc", b"\xd0\xcf\x11\xe0"),
        ],
    )
    def test_unsupported_types_rejected(
        self, client, admin_headers, sample_kb, real_process, filename, content
    ):
        resp = upload(client, admin_headers, sample_kb["id"], filename, content)
        assert resp.json()["data"][0]["success"] is False

    def test_empty_file_rejected(self, client, admin_headers, sample_kb, real_process):
        resp = upload(client, admin_headers, sample_kb["id"], "空的.md", b"")
        assert resp.json()["data"][0]["success"] is False
        assert "空文件" in resp.json()["data"][0]["message"]

    def test_oversized_file_rejected(
        self, client, admin_headers, sample_kb, real_process, monkeypatch
    ):
        """超过大小上限的文件要拒绝，避免把服务器磁盘塞满。"""
        from app.core import config

        monkeypatch.setattr(config.settings, "MAX_UPLOAD_SIZE_MB", 0)

        resp = upload(client, admin_headers, sample_kb["id"], "大文件.md", b"x" * 1024)
        result = resp.json()["data"][0]
        assert result["success"] is False
        assert "上限" in result["message"]

    def test_upload_to_nonexistent_kb(self, client, admin_headers, real_process):
        resp = upload(client, admin_headers, 99999, "a.md", SAMPLE_CONTENT.encode())
        assert resp.status_code == 404

    def test_path_traversal_filename_is_sanitized(
        self, client, admin_headers, sample_kb, real_process
    ):
        """文件名里的路径穿越尝试必须被清洗掉。

        如果直接把 "../../evil.md" 拼进存储路径，文件就写到项目外面去了。
        这里验证存下来的文件名只保留了最后一段。
        """
        resp = upload(
            client, admin_headers, sample_kb["id"], "../../../evil.md", SAMPLE_CONTENT.encode()
        )
        filename = resp.json()["data"][0]["document"]["filename"]

        assert "/" not in filename
        assert "\\" not in filename
        assert ".." not in filename
        assert filename == "evil.md"


# ============================================================
# 列表
# ============================================================
class TestListDocuments:
    def test_list_by_kb(self, client, admin_headers, sample_kb, real_process):
        upload(client, admin_headers, sample_kb["id"], "甲.md", "# 甲\n\n内容甲")
        upload(client, admin_headers, sample_kb["id"], "乙.md", "# 乙\n\n内容乙")

        resp = client.get(f"{API}/documents?kb_id={sample_kb['id']}", headers=admin_headers)
        assert resp.json()["data"]["total"] == 2

    def test_list_pagination(self, client, admin_headers, sample_kb, real_process):
        for i in range(5):
            upload(client, admin_headers, sample_kb["id"], f"文档{i}.md", f"# 文档{i}\n\n内容".encode())

        resp = client.get(
            f"{API}/documents?kb_id={sample_kb['id']}&page=1&page_size=2", headers=admin_headers
        )
        body = resp.json()["data"]
        assert len(body["items"]) == 2
        assert body["total"] == 5

    def test_filter_by_status(self, client, admin_headers, sample_kb, real_process):
        upload(client, admin_headers, sample_kb["id"], "待处理.md", "# 内容\n\n正文")

        pending = client.get(
            f"{API}/documents?kb_id={sample_kb['id']}&status=pending", headers=admin_headers
        )
        assert pending.json()["data"]["total"] == 1

        ready = client.get(
            f"{API}/documents?kb_id={sample_kb['id']}&status=ready", headers=admin_headers
        )
        assert ready.json()["data"]["total"] == 0

    def test_search_by_filename(self, client, admin_headers, sample_kb, real_process):
        upload(client, admin_headers, sample_kb["id"], "手机参数.md", "# 手机\n\n参数")
        upload(client, admin_headers, sample_kb["id"], "冰箱参数.md", "# 冰箱\n\n参数")

        resp = client.get(
            f"{API}/documents?kb_id={sample_kb['id']}&keyword=手机", headers=admin_headers
        )
        assert resp.json()["data"]["total"] == 1


# ============================================================
# 删除 / 重建
# ============================================================
class TestDeleteAndReindex:
    def test_delete_document(self, client, admin_headers, sample_kb, real_process):
        resp = upload(client, admin_headers, sample_kb["id"], "待删.md", "# 内容\n\n正文")
        doc_id = resp.json()["data"][0]["document"]["id"]

        assert client.delete(f"{API}/documents/{doc_id}", headers=admin_headers).status_code == 200
        assert client.get(f"{API}/documents?kb_id={sample_kb['id']}", headers=admin_headers).json()["data"]["total"] == 0

    def test_delete_updates_kb_stats(self, client, admin_headers, sample_kb, real_process):
        """删除文档后知识库的文档计数要跟着更新。"""
        resp = upload(client, admin_headers, sample_kb["id"], "计数.md", "# 内容\n\n正文")
        doc_id = resp.json()["data"][0]["document"]["id"]

        before = client.get(f"{API}/knowledge-bases/{sample_kb['id']}", headers=admin_headers).json()["data"]
        assert before["doc_count"] == 1

        client.delete(f"{API}/documents/{doc_id}", headers=admin_headers)

        after = client.get(f"{API}/knowledge-bases/{sample_kb['id']}", headers=admin_headers).json()["data"]
        assert after["doc_count"] == 0

    def test_reindex_resets_status(self, client, admin_headers, sample_kb, real_process):
        resp = upload(client, admin_headers, sample_kb["id"], "重建.md", "# 内容\n\n正文")
        doc_id = resp.json()["data"][0]["document"]["id"]

        reindex = client.post(f"{API}/documents/{doc_id}/reindex", headers=admin_headers)
        assert reindex.status_code == 200
        assert reindex.json()["data"]["status"] == "pending"

    def test_reindex_missing_file_rejected(
        self, client, admin_headers, sample_kb, real_process, db_session
    ):
        """原文件丢失时应该给出明确提示，而不是后台默默失败。"""
        resp = upload(client, admin_headers, sample_kb["id"], "丢文件.md", "# 内容\n\n正文")
        doc_id = resp.json()["data"][0]["document"]["id"]

        # 模拟原文件被误删
        from pathlib import Path

        from app.models.document import Document

        doc = db_session.get(Document, doc_id)
        Path(doc.stored_path).unlink(missing_ok=True)

        resp = client.post(f"{API}/documents/{doc_id}/reindex", headers=admin_headers)
        assert resp.status_code == 400
        assert "丢失" in resp.json()["message"]

    def test_delete_nonexistent_returns_404(self, client, admin_headers):
        assert client.delete(f"{API}/documents/99999", headers=admin_headers).status_code == 404


# ============================================================
# 分块预览
# ============================================================
class TestChunks:
    def test_chunks_of_unprocessed_document_is_empty(
        self, client, admin_headers, sample_kb, real_process
    ):
        """还没处理完的文档没有分块，不该报错，返回空列表就行。"""
        resp = upload(client, admin_headers, sample_kb["id"], "待处理.md", "# 内容\n\n正文")
        doc_id = resp.json()["data"][0]["document"]["id"]

        resp = client.get(f"{API}/documents/{doc_id}/chunks", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["total"] == 0
        assert resp.json()["data"]["items"] == []

    def test_chunks_of_nonexistent_document(self, client, admin_headers):
        assert client.get(f"{API}/documents/99999/chunks", headers=admin_headers).status_code == 404
