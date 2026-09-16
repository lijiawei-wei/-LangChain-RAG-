"""知识库管理接口测试。

重点验证两件事：
  1. 增删改查功能本身正确
  2. **权限隔离** —— 普通用户绝对不能管理知识库，这是需求里的硬性要求
"""

import pytest

API = "/api/v1"


# ============================================================
# 权限隔离（最重要的一组）
# ============================================================
class TestKnowledgeBasePermissions:
    def test_anonymous_cannot_list(self, client):
        """没登录不能看知识库列表。"""
        assert client.get(f"{API}/knowledge-bases").status_code == 401

    def test_normal_user_can_list(self, client, auth_headers):
        """普通用户能看列表——提问时要选知识库，必须读得到。"""
        assert client.get(f"{API}/knowledge-bases", headers=auth_headers).status_code == 200

    def test_normal_user_cannot_create(self, client, auth_headers):
        """普通用户不能新建知识库。"""
        resp = client.post(
            f"{API}/knowledge-bases", headers=auth_headers, json={"name": "偷偷建的"}
        )
        assert resp.status_code == 403

    def test_normal_user_cannot_update(self, client, auth_headers, sample_kb):
        resp = client.patch(
            f"{API}/knowledge-bases/{sample_kb['id']}",
            headers=auth_headers,
            json={"name": "改个名字"},
        )
        assert resp.status_code == 403

    def test_normal_user_cannot_delete(self, client, auth_headers, sample_kb):
        resp = client.delete(f"{API}/knowledge-bases/{sample_kb['id']}", headers=auth_headers)
        assert resp.status_code == 403

    def test_normal_user_cannot_read_status(self, client, auth_headers, sample_kb):
        resp = client.get(f"{API}/knowledge-bases/{sample_kb['id']}/status", headers=auth_headers)
        assert resp.status_code == 403


# ============================================================
# 新建
# ============================================================
class TestCreateKnowledgeBase:
    def test_create_success(self, client, admin_headers):
        resp = client.post(
            f"{API}/knowledge-bases",
            headers=admin_headers,
            json={"name": "手机数码", "description": "手机相关商品资料"},
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["name"] == "手机数码"
        assert data["doc_count"] == 0
        assert data["chunk_count"] == 0

    def test_duplicate_name_rejected(self, client, admin_headers):
        """同名知识库要拒绝——用户在界面上无法区分两个同名库。"""
        payload = {"name": "重名测试"}
        assert client.post(f"{API}/knowledge-bases", headers=admin_headers, json=payload).status_code == 201

        resp = client.post(f"{API}/knowledge-bases", headers=admin_headers, json=payload)
        assert resp.status_code == 409
        assert "已经有一个叫" in resp.json()["message"]

    @pytest.mark.parametrize("name", ["", "   ", "x" * 101])
    def test_invalid_name_rejected(self, client, admin_headers, name):
        resp = client.post(
            f"{API}/knowledge-bases", headers=admin_headers, json={"name": name}
        )
        assert resp.status_code == 422

    def test_name_is_trimmed(self, client, admin_headers):
        """前后空格要去掉，否则「手机」和「手机 」会被当成两个不同的库。"""
        resp = client.post(
            f"{API}/knowledge-bases", headers=admin_headers, json={"name": "  带空格的  "}
        )
        assert resp.json()["data"]["name"] == "带空格的"


# ============================================================
# 查询
# ============================================================
class TestListKnowledgeBases:
    def test_list_is_paginated(self, client, admin_headers):
        for i in range(5):
            client.post(f"{API}/knowledge-bases", headers=admin_headers, json={"name": f"库{i}"})

        resp = client.get(
            f"{API}/knowledge-bases?page=1&page_size=2", headers=admin_headers
        )
        body = resp.json()["data"]
        assert len(body["items"]) == 2
        assert body["total"] == 5
        assert body["page"] == 1

    def test_list_keyword_search(self, client, admin_headers):
        client.post(f"{API}/knowledge-bases", headers=admin_headers, json={"name": "手机数码"})
        client.post(f"{API}/knowledge-bases", headers=admin_headers, json={"name": "家用电器"})

        resp = client.get(f"{API}/knowledge-bases?keyword=手机", headers=admin_headers)
        body = resp.json()["data"]
        assert body["total"] == 1
        assert body["items"][0]["name"] == "手机数码"

    def test_list_all_unpaginated(self, client, admin_headers):
        for i in range(3):
            client.post(f"{API}/knowledge-bases", headers=admin_headers, json={"name": f"全部{i}"})

        resp = client.get(f"{API}/knowledge-bases/all", headers=admin_headers)
        assert len(resp.json()["data"]) == 3

    def test_get_nonexistent_returns_404(self, client, admin_headers):
        assert (
            client.get(f"{API}/knowledge-bases/99999", headers=admin_headers).status_code == 404
        )

    def test_page_size_upper_bound(self, client, admin_headers):
        """每页条数有上限，防止有人传 page_size=999999 把数据库拖垮。"""
        resp = client.get(f"{API}/knowledge-bases?page_size=999999", headers=admin_headers)
        assert resp.status_code == 422


# ============================================================
# 修改
# ============================================================
class TestUpdateKnowledgeBase:
    def test_rename_success(self, client, admin_headers, sample_kb):
        resp = client.patch(
            f"{API}/knowledge-bases/{sample_kb['id']}",
            headers=admin_headers,
            json={"name": "新名字"},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] == "新名字"

    def test_rename_to_existing_name_rejected(self, client, admin_headers, sample_kb):
        """改成别人已经在用的名字要拒绝。"""
        client.post(f"{API}/knowledge-bases", headers=admin_headers, json={"name": "已占用的名字"})

        resp = client.patch(
            f"{API}/knowledge-bases/{sample_kb['id']}",
            headers=admin_headers,
            json={"name": "已占用的名字"},
        )
        assert resp.status_code == 409

    def test_rename_to_same_name_allowed(self, client, admin_headers, sample_kb):
        """改成自己现在的名字应该放行——这不是冲突，用户可能只改了描述。"""
        resp = client.patch(
            f"{API}/knowledge-bases/{sample_kb['id']}",
            headers=admin_headers,
            json={"name": sample_kb["name"], "description": "只改描述"},
        )
        assert resp.status_code == 200

    def test_update_description_only(self, client, admin_headers, sample_kb):
        resp = client.patch(
            f"{API}/knowledge-bases/{sample_kb['id']}",
            headers=admin_headers,
            json={"description": "新描述"},
        )
        assert resp.json()["data"]["description"] == "新描述"
        assert resp.json()["data"]["name"] == sample_kb["name"]


# ============================================================
# 删除
# ============================================================
class TestDeleteKnowledgeBase:
    def test_delete_success(self, client, admin_headers, sample_kb):
        resp = client.delete(f"{API}/knowledge-bases/{sample_kb['id']}", headers=admin_headers)
        assert resp.status_code == 200
        # 删完再查应该是 404
        assert (
            client.get(f"{API}/knowledge-bases/{sample_kb['id']}", headers=admin_headers).status_code
            == 404
        )

    def test_delete_nonexistent_returns_404(self, client, admin_headers):
        assert (
            client.delete(f"{API}/knowledge-bases/99999", headers=admin_headers).status_code == 404
        )

    def test_delete_cascades_to_documents(self, client, admin_headers, sample_kb, real_process):
        """删除知识库要连带删掉它下面的文档记录，不能留下孤儿数据。"""
        kb_id = sample_kb["id"]
        client.post(
            f"{API}/documents/upload",
            headers=admin_headers,
            data={"kb_id": str(kb_id)},
            files={"files": ("商品.md", "# 测试内容\n\n这是一段测试文档内容。", "text/markdown")},
        )
        assert client.get(f"{API}/documents?kb_id={kb_id}", headers=admin_headers).json()["data"]["total"] == 1

        client.delete(f"{API}/knowledge-bases/{kb_id}", headers=admin_headers)

        # 知识库没了，它下面的文档也应该查不到了
        assert client.get(f"{API}/documents?kb_id={kb_id}", headers=admin_headers).json()["data"]["total"] == 0
