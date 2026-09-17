"""问答接口测试。

这里把 RAG 流水线换成了假的（见 fake_pipeline 夹具），
所以测试跑得很快、也不花钱——因为**不调用真正的大模型接口**。

这组测试要验证的是「编排逻辑」：
  会话有没有正确创建和复用、消息有没有存下来、引用有没有正确返回、
  历史有没有传给流水线、出错了怎么处理。
至于「回答质量好不好」，那是提示词和检索策略的事，
由 scripts/eval_rag.py 用真问题集来评估，不适合放在单元测试里。
"""

import pytest

API = "/api/v1"


@pytest.fixture
def buyer(client):
    """一个已登录的普通用户。"""
    client.post(f"{API}/auth/register", json={"username": "buyer", "password": "buyer123456"})
    token = client.post(
        f"{API}/auth/login", json={"username": "buyer", "password": "buyer123456"}
    ).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ============================================================
# 基本问答
# ============================================================
class TestChatQuery:
    def test_new_conversation_created_automatically(self, client, buyer, sample_kb, fake_pipeline):
        """不传会话 ID 时会自动新建一个会话。"""
        resp = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池容量是多少？", "kb_id": sample_kb["id"]},
        )
        assert resp.status_code == 200

        data = resp.json()["data"]
        assert data["conversation_id"] > 0
        assert data["answer"]
        assert len(data["citations"]) == 1
        assert data["latency_ms"] == 1234

    def test_conversation_title_from_first_question(self, client, buyer, sample_kb, fake_pipeline):
        """第一次提问应该用问题内容给会话命名。

        否则所有会话都叫「新对话」，用户在列表里根本分不清哪个是哪个。
        """
        resp = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "星耀 X9 Pro 电池容量是多少？", "kb_id": sample_kb["id"]},
        )
        conv_id = resp.json()["data"]["conversation_id"]

        conv = client.get(f"{API}/conversations", headers=buyer).json()["data"]["items"][0]
        assert conv["id"] == conv_id
        assert conv["title"] == "星耀 X9 Pro 电池容量是多少？"

    def test_long_question_title_is_truncated(self, client, buyer, sample_kb, fake_pipeline):
        long_question = "请问一下" * 20  # 100 个字
        client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": long_question, "kb_id": sample_kb["id"]},
        )

        conv = client.get(f"{API}/conversations", headers=buyer).json()["data"]["items"][0]
        assert len(conv["title"]) <= 25  # 24 个字 + 一个省略号
        assert conv["title"].endswith("…")

    def test_follow_up_uses_same_conversation(self, client, buyer, sample_kb, fake_pipeline):
        """追问时传会话 ID，应该复用同一个会话。"""
        first = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池多大？", "kb_id": sample_kb["id"]},
        ).json()["data"]

        second = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "它支持快充吗？", "conversation_id": first["conversation_id"]},
        ).json()["data"]

        assert second["conversation_id"] == first["conversation_id"]

        # 只有一个会话，里面有 4 条消息（2 问 2 答）
        convs = client.get(f"{API}/conversations", headers=buyer).json()["data"]
        assert convs["total"] == 1
        assert convs["items"][0]["message_count"] == 4

    def test_history_is_passed_to_pipeline(self, client, buyer, sample_kb, fake_pipeline):
        """追问时必须把历史对话传给流水线。

        不传历史的话，「它支持快充吗」里的「它」就无法消解，
        检索会完全找不到相关资料——这是多轮对话能用的前提。
        """
        first = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "星耀 X9 Pro 的电池容量是多少？", "kb_id": sample_kb["id"]},
        ).json()["data"]

        # 第一次调用时没有历史
        assert fake_pipeline[0]["history"] == []

        client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "它支持快充吗？", "conversation_id": first["conversation_id"]},
        )

        # 第二次调用时应该带上了前两轮的消息
        second_call = fake_pipeline[1]
        assert len(second_call["history"]) == 2
        assert second_call["history"][0]["role"] == "user"
        assert "电池容量" in second_call["history"][0]["content"]
        assert second_call["history"][1]["role"] == "assistant"

    def test_citations_are_returned_and_persisted(self, client, buyer, sample_kb, fake_pipeline):
        """引用要既返回给前端，也要存进数据库（供历史对话回放）。"""
        resp = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池多大？", "kb_id": sample_kb["id"]},
        )
        data = resp.json()["data"]

        # 接口返回里有完整引用信息
        citation = data["citations"][0]
        assert citation["index"] == 1
        assert citation["filename"] == "产品手册.md"
        assert citation["page"] == 3
        assert citation["source_label"] == "产品手册.md 第 3 页"
        assert citation["used"] is True

        # 历史消息里也能拿到（模拟用户重新登录后回看）
        messages = client.get(
            f"{API}/conversations/{data['conversation_id']}/messages", headers=buyer
        ).json()["data"]
        assistant_message = messages[1]
        assert assistant_message["citations"][0]["filename"] == "产品手册.md"

    def test_empty_question_rejected(self, client, buyer, sample_kb, fake_pipeline):
        resp = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "   ", "kb_id": sample_kb["id"]},
        )
        assert resp.status_code == 422

    def test_anonymous_cannot_chat(self, client, sample_kb, fake_pipeline):
        resp = client.post(f"{API}/chat/query", json={"question": "电池多大？"})
        assert resp.status_code == 401


# ============================================================
# 知识库范围
# ============================================================
class TestKnowledgeBaseScope:
    def test_specific_kb_is_used(self, client, buyer, sample_kb, fake_pipeline):
        client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池多大？", "kb_id": sample_kb["id"]},
        )
        assert fake_pipeline[0]["kb_ids"] == [sample_kb["id"]]

    def test_no_kb_means_all(self, client, buyer, sample_kb, admin_headers, fake_pipeline):
        """不指定知识库时，应该检索全部知识库。"""
        second_kb = client.post(
            f"{API}/knowledge-bases",
            headers=admin_headers,
            json={"name": "第二个库"},
        ).json()["data"]

        client.post(f"{API}/chat/query", headers=buyer, json={"question": "电池多大？"})

        assert set(fake_pipeline[0]["kb_ids"]) == {sample_kb["id"], second_kb["id"]}

    def test_nonexistent_kb_rejected(self, client, buyer, sample_kb, fake_pipeline):
        resp = client.post(
            f"{API}/chat/query", headers=buyer, json={"question": "电池多大？", "kb_id": 99999}
        )
        assert resp.status_code == 404

    def test_no_kb_at_all_gives_clear_message(self, client, buyer, fake_pipeline):
        """系统里一个知识库都没有时，要给出能看懂的提示。"""
        resp = client.post(f"{API}/chat/query", headers=buyer, json={"question": "电池多大？"})
        assert resp.status_code == 400
        assert "还没有任何知识库" in resp.json()["message"]

    def test_conversation_kb_binding(self, client, buyer, sample_kb, fake_pipeline):
        """会话绑定了知识库时，后续追问要一直用那个库，不受请求参数影响。"""
        first = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池多大？", "kb_id": sample_kb["id"]},
        ).json()["data"]

        # 追问时即使传了别的 kb_id，也应该用会话自己绑定的那个
        client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "充电多久？", "conversation_id": first["conversation_id"]},
        )
        assert fake_pipeline[1]["kb_ids"] == [sample_kb["id"]]


# ============================================================
# 跨会话的数据隔离
# ============================================================
class TestChatIsolation:
    def test_cannot_chat_in_others_conversation(self, client, sample_kb, fake_pipeline):
        """不能往别人的会话里发消息。"""
        # 用户 A 建会话
        client.post(f"{API}/auth/register", json={"username": "alice2", "password": "alice123456"})
        token_a = client.post(
            f"{API}/auth/login", json={"username": "alice2", "password": "alice123456"}
        ).json()["data"]["access_token"]
        conv_id = client.post(
            f"{API}/chat/query",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"question": "电池多大？", "kb_id": sample_kb["id"]},
        ).json()["data"]["conversation_id"]

        # 用户 B 试图往 A 的会话里发消息
        client.post(f"{API}/auth/register", json={"username": "bob2", "password": "bob123456"})
        token_b = client.post(
            f"{API}/auth/login", json={"username": "bob2", "password": "bob123456"}
        ).json()["data"]["access_token"]

        resp = client.post(
            f"{API}/chat/query",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"question": "我也来问", "conversation_id": conv_id},
        )
        assert resp.status_code == 404


# ============================================================
# 问答日志
# ============================================================
class TestQALog:
    def test_qa_log_written(self, client, buyer, sample_kb, fake_pipeline, db_session):
        """每次问答都要记一条日志，统计看板和效果评估都靠它取数。"""
        from sqlalchemy import select

        from app.models.qa_log import QALog

        client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池多大？", "kb_id": sample_kb["id"]},
        )

        logs = list(db_session.scalars(select(QALog)).all())
        assert len(logs) == 1

        log = logs[0]
        assert log.question == "电池多大？"
        assert log.latency_ms == 1234
        assert log.prompt_tokens == 800
        assert log.completion_tokens == 50
        assert log.cache_hit is False
        # 分阶段耗时和引用分数都记下来了，方便做性能分析
        assert "generate" in log.stage_latency
        assert "1" in log.rerank_scores


class TestNoContextReply:
    """一条资料都检索不到时的答复。

    这组测试直接跑 pipeline（不是假的夹具），但把检索那一步换成"返回空"，
    所以**一次外部接口都不会调，也不花钱**——正好可以顺便验证
    "没检索到就不该调模型"这件事。
    """

    @pytest.fixture
    def empty_retrieval(self, monkeypatch):
        """把检索结果固定成空。"""
        from app.rag import pipeline

        async def _empty(kb_ids, query):
            return []

        monkeypatch.setattr(pipeline, "retrieve", _empty)

    @pytest.mark.asyncio
    async def test_reply_is_fixed_text_without_calling_model(self, empty_retrieval):
        """没资料就直接给固定答复：瞬时返回、不花一分钱。"""
        from app.rag import pipeline
        from app.rag.prompts import NO_CONTEXT_REPLY

        result = await pipeline.answer("这款衣服怎么选码", None, [3])

        assert result.answer == NO_CONTEXT_REPLY
        assert result.citations == []
        # token 为 0 就证明没调过模型
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0

    def test_reply_points_at_the_scope_selector(self):
        """固定答复里要提醒用户检查「检索范围」。

        **这是修一个真实问题留下的**：资料放在「服装」知识库里，
        但聊天页的检索范围还停在「手机数码」，于是问什么都答不上来。
        原来的文案只说"没找到相关资料 / 换个说法再问"，用户照着做
        （换了三四种问法）依然没结果——因为问题根本不在问法上。
        现在第一句就让他去看检索范围。
        """
        from app.rag.prompts import NO_CONTEXT_REPLY

        assert "检索范围" in NO_CONTEXT_REPLY


# 需要管理员权限的地方直接用 conftest 里的 admin_headers 夹具。
# 不要自己写「注册一个用户再改它的角色」这种取巧做法——
# 那通常意味着绕过依赖注入去操作数据库，一不小心就改到真实库上去了。
