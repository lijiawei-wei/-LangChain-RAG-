"""会话管理接口测试。

重点验证 **数据隔离** —— 需求里明确要求「每个用户有每个用户的独立会话」，
这组测试就是用来保证这条要求的。
"""

import pytest

API = "/api/v1"


@pytest.fixture
def user_a(client):
    """用户 A，返回请求头。"""
    client.post(f"{API}/auth/register", json={"username": "usera", "password": "usera123456"})
    token = client.post(
        f"{API}/auth/login", json={"username": "usera", "password": "usera123456"}
    ).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def user_b(client):
    """用户 B，返回请求头。"""
    client.post(f"{API}/auth/register", json={"username": "userb", "password": "userb123456"})
    token = client.post(
        f"{API}/auth/login", json={"username": "userb", "password": "userb123456"}
    ).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 数据隔离（最重要）
# ============================================================
class TestConversationIsolation:
    def test_user_only_sees_own_conversations(self, client, user_a, user_b):
        """A 建的会话，B 在列表里看不到。"""
        client.post(f"{API}/conversations", headers=user_a, json={"title": "A的会话"})
        client.post(f"{API}/conversations", headers=user_a, json={"title": "A的会话2"})
        client.post(f"{API}/conversations", headers=user_b, json={"title": "B的会话"})

        list_a = client.get(f"{API}/conversations", headers=user_a).json()["data"]
        list_b = client.get(f"{API}/conversations", headers=user_b).json()["data"]

        assert list_a["total"] == 2
        assert list_b["total"] == 1
        assert all(c["title"].startswith("A的") for c in list_a["items"])
        assert list_b["items"][0]["title"] == "B的会话"

    def test_cannot_read_others_conversation_messages(self, client, user_a, user_b):
        """B 拿 A 的会话 ID 去读消息，必须失败。"""
        conv_id = client.post(
            f"{API}/conversations", headers=user_a, json={"title": "A的私密会话"}
        ).json()["data"]["id"]

        resp = client.get(f"{API}/conversations/{conv_id}/messages", headers=user_b)
        assert resp.status_code == 404

    def test_cannot_rename_others_conversation(self, client, user_a, user_b):
        conv_id = client.post(
            f"{API}/conversations", headers=user_a, json={"title": "原标题"}
        ).json()["data"]["id"]

        resp = client.patch(
            f"{API}/conversations/{conv_id}", headers=user_b, json={"title": "被改了"}
        )
        assert resp.status_code == 404

        # 确认真的没被改
        after = client.get(f"{API}/conversations", headers=user_a).json()["data"]["items"][0]
        assert after["title"] == "原标题"

    def test_cannot_delete_others_conversation(self, client, user_a, user_b):
        conv_id = client.post(
            f"{API}/conversations", headers=user_a, json={"title": "别删我"}
        ).json()["data"]["id"]

        assert (
            client.delete(f"{API}/conversations/{conv_id}", headers=user_b).status_code == 404
        )
        # A 的会话还在
        assert client.get(f"{API}/conversations", headers=user_a).json()["data"]["total"] == 1

    def test_clear_only_affects_own(self, client, user_a, user_b):
        """清空自己的会话，不能影响到别人的。"""
        client.post(f"{API}/conversations", headers=user_a, json={"title": "A1"})
        client.post(f"{API}/conversations", headers=user_a, json={"title": "A2"})
        client.post(f"{API}/conversations", headers=user_b, json={"title": "B1"})

        resp = client.delete(f"{API}/conversations", headers=user_a)
        assert resp.json()["data"]["deleted"] == 2

        assert client.get(f"{API}/conversations", headers=user_a).json()["data"]["total"] == 0
        assert client.get(f"{API}/conversations", headers=user_b).json()["data"]["total"] == 1

    def test_anonymous_cannot_access(self, client):
        assert client.get(f"{API}/conversations").status_code == 401


# ============================================================
# 基本增删改查
# ============================================================
class TestConversationCrud:
    def test_create_with_default_title(self, client, user_a):
        resp = client.post(f"{API}/conversations", headers=user_a, json={})
        assert resp.status_code == 201
        assert resp.json()["data"]["title"] == "新对话"
        assert resp.json()["data"]["message_count"] == 0

    def test_create_with_custom_title_and_kb(self, client, user_a, sample_kb):
        resp = client.post(
            f"{API}/conversations",
            headers=user_a,
            json={"title": "我的咨询", "kb_id": sample_kb["id"]},
        )
        data = resp.json()["data"]
        assert data["title"] == "我的咨询"
        assert data["kb_id"] == sample_kb["id"]

    def test_rename(self, client, user_a):
        conv_id = client.post(f"{API}/conversations", headers=user_a, json={}).json()["data"]["id"]

        resp = client.patch(
            f"{API}/conversations/{conv_id}", headers=user_a, json={"title": "改过的名字"}
        )
        assert resp.json()["data"]["title"] == "改过的名字"

    def test_rename_to_empty_rejected(self, client, user_a):
        conv_id = client.post(f"{API}/conversations", headers=user_a, json={}).json()["data"]["id"]
        resp = client.patch(
            f"{API}/conversations/{conv_id}", headers=user_a, json={"title": "   "}
        )
        assert resp.status_code == 422

    def test_delete(self, client, user_a):
        conv_id = client.post(f"{API}/conversations", headers=user_a, json={}).json()["data"]["id"]

        assert client.delete(f"{API}/conversations/{conv_id}", headers=user_a).status_code == 200
        assert client.get(f"{API}/conversations", headers=user_a).json()["data"]["total"] == 0

    def test_list_is_paginated(self, client, user_a):
        for i in range(5):
            client.post(f"{API}/conversations", headers=user_a, json={"title": f"会话{i}"})

        resp = client.get(f"{API}/conversations?page=1&page_size=2", headers=user_a)
        body = resp.json()["data"]
        assert len(body["items"]) == 2
        assert body["total"] == 5

    def test_search_by_title(self, client, user_a):
        client.post(f"{API}/conversations", headers=user_a, json={"title": "手机咨询"})
        client.post(f"{API}/conversations", headers=user_a, json={"title": "冰箱咨询"})

        resp = client.get(f"{API}/conversations?keyword=手机", headers=user_a)
        assert resp.json()["data"]["total"] == 1

    def test_get_messages_of_empty_conversation(self, client, user_a):
        conv_id = client.post(f"{API}/conversations", headers=user_a, json={}).json()["data"]["id"]

        resp = client.get(f"{API}/conversations/{conv_id}/messages", headers=user_a)
        assert resp.status_code == 200
        assert resp.json()["data"] == []


# ============================================================
# 消息持久化与恢复
# ============================================================
class TestMessagePersistence:
    def test_messages_can_be_restored(self, client, user_a, db_session):
        """模拟「退出后重新登录还能看到历史对话」。

        这里直接往数据库塞消息，不经过问答接口——
        因为问答会真的调用大模型，那是 test_chat.py 用假流水线测的部分。
        """
        from app.models.message import Message
        from app.services import conversation_service

        conv = conversation_service.create_conversation(db_session, user_id=1, title="历史测试")
        conversation_service.append_message(db_session, conv.id, "user", "电池多大？")
        conversation_service.append_message(
            db_session,
            conv.id,
            "assistant",
            "5000mAh [1]。",
            citations=[{"index": 1, "filename": "手册.md", "text": "5000mAh", "used": True}],
            latency_ms=1200,
            prompt_tokens=100,
            completion_tokens=20,
        )

        # 模拟用户重新登录后的拉取
        resp = client.get(f"{API}/conversations/{conv.id}/messages", headers=user_a)
        messages = resp.json()["data"]

        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "电池多大？"

        assert messages[1]["role"] == "assistant"
        # 引用被正确解析成了对象列表
        assert messages[1]["citations"][0]["filename"] == "手册.md"
        assert messages[1]["latency_ms"] == 1200

    def test_message_count_updates(self, client, user_a, db_session):
        from app.services import conversation_service

        conv = conversation_service.create_conversation(db_session, user_id=1, title="计数")
        assert conv.message_count == 0

        conversation_service.append_message(db_session, conv.id, "user", "问题一")
        conversation_service.append_message(db_session, conv.id, "assistant", "回答一")

        db_session.refresh(conv)
        assert conv.message_count == 2

    def test_cursor_pagination(self, client, user_a, db_session):
        """游标分页：往上翻历史时不能出现重复消息。

        这是聊天记录分页的经典坑——用页码分页的话，新消息一进来
        "第 2 页"的内容就往后错位了。
        """
        from app.services import conversation_service

        conv = conversation_service.create_conversation(db_session, user_id=1, title="翻页")
        for i in range(10):
            conversation_service.append_message(db_session, conv.id, "user", f"第{i}条")

        # 取最近 4 条
        page1 = client.get(
            f"{API}/conversations/{conv.id}/messages?limit=4", headers=user_a
        ).json()["data"]
        assert [m["content"] for m in page1] == ["第6条", "第7条", "第8条", "第9条"]

        # 往前翻，取比第一条更早的
        oldest_id = page1[0]["id"]
        page2 = client.get(
            f"{API}/conversations/{conv.id}/messages?limit=4&before_id={oldest_id}",
            headers=user_a,
        ).json()["data"]
        assert [m["content"] for m in page2] == ["第2条", "第3条", "第4条", "第5条"]

        # 两页之间没有任何重复
        assert not ({m["id"] for m in page1} & {m["id"] for m in page2})
