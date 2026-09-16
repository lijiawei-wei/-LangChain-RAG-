"""流式问答（SSE）接口测试。

流水线被换成了假的（见 fake_stream 夹具），所以不调真实模型、不花钱。

这组测试验证的是 **SSE 协议本身**：
事件顺序对不对、数据格式对不对、出错了能不能被前端感知、
流结束后消息有没有正确落库。
至于"首字延迟多少毫秒"这种性能指标，得用真模型测，
在 scripts/ 下另有脚本，不适合放进单元测试。
"""

import json

import pytest

from app.rag import pipeline

API = "/api/v1"

FAKE_ANSWER_PIECES = ["星耀 X9 Pro", "的电池容量", "是 5000mAh", " [1]。"]


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析成 (事件名, 数据) 列表。

    这个解析逻辑和前端 utils/sse.js 里的是同一套规则，
    测试里自己实现一遍能顺便验证协议格式确实是标准的。
    """
    events = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue

        event_name = None
        data_lines = []
        for line in block.split("\n"):
            if line.startswith("event: "):
                event_name = line[7:]
            elif line.startswith("data: "):
                data_lines.append(line[6:])

        if event_name and data_lines:
            events.append((event_name, json.loads("".join(data_lines))))

    return events


@pytest.fixture
def fake_stream(monkeypatch):
    """把流式流水线换成假的。"""
    calls = []

    async def _fake_answer_stream(question, history, kb_ids, cancel_event=None):
        calls.append({"question": question, "history": history, "kb_ids": kb_ids})

        yield "prepared", {
            "query": question,
            "citations": [
                {
                    "index": 1,
                    "chunk_id": "doc1_c2",
                    "doc_id": 1,
                    "filename": "产品手册.md",
                    "page": 3,
                    "source_label": "产品手册.md 第 3 页",
                    "score": 0.8123,
                    "text": "内置 5000mAh 大容量电池。",
                    "truncated": False,
                    "used": False,  # 还没生成回答，此时还不知道有没有被引用
                }
            ],
            "stage_latency": {"rewrite": 10, "retrieve": 200},
            "is_empty": False,
        }

        for piece in FAKE_ANSWER_PIECES:
            yield "token", {"delta": piece}

        yield "finished", {
            "answer": "".join(FAKE_ANSWER_PIECES),
            "citations": [
                {
                    "index": 1,
                    "chunk_id": "doc1_c2",
                    "doc_id": 1,
                    "filename": "产品手册.md",
                    "page": 3,
                    "source_label": "产品手册.md 第 3 页",
                    "score": 0.8123,
                    "text": "内置 5000mAh 大容量电池。",
                    "truncated": False,
                    "used": True,  # 生成完毕后标成已引用
                }
            ],
            "query": question,
            "latency_ms": 1500,
            "stage_latency": {"rewrite": 10, "retrieve": 200, "generate": 1200},
            "prompt_tokens": 900,
            "completion_tokens": 30,
        }

    monkeypatch.setattr(pipeline, "answer_stream", _fake_answer_stream)
    return calls


@pytest.fixture
def buyer(client):
    client.post(f"{API}/auth/register", json={"username": "streambuyer", "password": "stream123456"})
    token = client.post(
        f"{API}/auth/login", json={"username": "streambuyer", "password": "stream123456"}
    ).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def do_stream(client, headers, payload):
    return client.post(f"{API}/chat/stream", headers=headers, json=payload)


# ============================================================
# 事件协议
# ============================================================
class TestSseProtocol:
    def test_response_headers(self, client, buyer, sample_kb, fake_stream):
        """SSE 响应必须带上正确的头，否则会被中间层缓冲、退化成假流式。"""
        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        # 这两行分别告诉 nginx 和浏览器"别缓冲"
        assert resp.headers.get("x-accel-buffering") == "no"
        assert "no-cache" in resp.headers.get("cache-control", "")

    def test_event_order(self, client, buyer, sample_kb, fake_stream):
        """事件顺序必须是 meta → sources → token... → done。

        特别是 sources 必须在 token 之前：引用来源先到，
        前端就能提前把右侧面板渲染出来，用户等文字的时候不至于干瞪眼。
        """
        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})
        events = parse_sse(resp.text)
        names = [name for name, _ in events]

        assert names[0] == "meta"
        assert names[1] == "sources"
        assert names[-1] == "done"
        assert names.count("token") == len(FAKE_ANSWER_PIECES)

    def test_meta_contains_conversation_info(self, client, buyer, sample_kb, fake_stream):
        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})
        events = dict(parse_sse(resp.text))

        meta = events["meta"]
        assert meta["conversation_id"] > 0
        assert meta["user_message_id"] > 0
        # 标题取自第一个问题
        assert meta["title"] == "电池多大？"

    def test_sources_before_tokens_with_citations(self, client, buyer, sample_kb, fake_stream):
        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})
        events = parse_sse(resp.text)

        # 找到 sources 和第一个 token 的位置
        names = [name for name, _ in events]
        sources_pos = names.index("sources")
        first_token_pos = names.index("token")
        assert sources_pos < first_token_pos

        sources = events[sources_pos][1]
        assert len(sources["citations"]) == 1
        assert sources["citations"][0]["filename"] == "产品手册.md"
        # 检索刚完成时还不知道有没有被引用，应该是 False
        assert sources["citations"][0]["used"] is False

    def test_tokens_concatenate_to_full_answer(self, client, buyer, sample_kb, fake_stream):
        """把收到的所有 token 拼起来，必须等于最终答案。"""
        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})
        events = parse_sse(resp.text)

        streamed = "".join(data["delta"] for name, data in events if name == "token")
        final = dict(events)["done"]["answer"]

        assert streamed == final
        assert "5000mAh" in streamed

    def test_done_contains_final_stats(self, client, buyer, sample_kb, fake_stream):
        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})
        done = dict(parse_sse(resp.text))["done"]

        assert done["assistant_message_id"] > 0
        assert done["latency_ms"] == 1500
        assert done["prompt_tokens"] == 900
        assert done["completion_tokens"] == 30
        # 生成完成后，引用应该已经标成 used=True
        assert done["citations"][0]["used"] is True

    def test_data_is_valid_json_with_chinese(self, client, buyer, sample_kb, fake_stream):
        """中文必须原样传输，不能被转义成 \\uXXXX。

        转义虽然也能解析，但会让抓包调试时完全看不懂，
        而且体积更大。
        """
        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})
        assert "5000mAh" in resp.text
        assert "\\u" not in resp.text


# ============================================================
# 持久化
# ============================================================
class TestStreamPersistence:
    def test_messages_saved_after_stream(self, client, buyer, sample_kb, fake_stream):
        """流式结束后，提问和回答都要落库。

        这是「退出后重新登录还能看到历史」的前提。
        """
        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})
        done = dict(parse_sse(resp.text))["done"]
        conv_id = done["conversation_id"]

        messages = client.get(f"{API}/conversations/{conv_id}/messages", headers=buyer).json()["data"]

        assert len(messages) == 2
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "电池多大？"
        assert messages[1]["role"] == "assistant"
        assert "5000mAh" in messages[1]["content"]
        # 引用快照也存下来了
        assert messages[1]["citations"][0]["filename"] == "产品手册.md"
        assert messages[1]["citations"][0]["used"] is True

    def test_qa_log_written(self, client, buyer, sample_kb, fake_stream, db_session):
        from sqlalchemy import select

        from app.models.qa_log import QALog

        do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})

        logs = list(db_session.scalars(select(QALog)).all())
        assert len(logs) == 1
        assert logs[0].question == "电池多大？"
        assert logs[0].latency_ms == 1500

    def test_follow_up_stream_uses_history(self, client, buyer, sample_kb, fake_stream):
        """流式路径也必须带历史，否则多轮追问照样理解不了指代词。"""
        first = dict(
            parse_sse(do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]}).text)
        )
        conv_id = first["meta"]["conversation_id"]

        do_stream(client, buyer, {"question": "它支持快充吗？", "conversation_id": conv_id})

        # 第二次调用时应该带上了前两轮
        assert len(fake_stream[1]["history"]) == 2
        assert fake_stream[1]["history"][0]["role"] == "user"


# ============================================================
# 异常处理
# ============================================================
class TestStreamErrorHandling:
    def test_error_becomes_sse_event_not_broken_connection(
        self, client, buyer, sample_kb, monkeypatch
    ):
        """流水线抛异常时，必须转成 error 事件推给前端。

        如果任由异常抛出，连接会直接断掉，前端只能看到"网络错误"，
        既不知道原因，界面也可能永远转圈。
        """
        async def _boom(question, history, kb_ids, cancel_event=None):
            yield "prepared", {"query": question, "citations": [], "stage_latency": {}, "is_empty": False}
            raise RuntimeError("模型服务不可用")

        monkeypatch.setattr(pipeline, "answer_stream", _boom)

        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})

        # 连接必须是正常结束的（HTTP 200），错误信息在事件里
        assert resp.status_code == 200

        events = parse_sse(resp.text)
        names = [name for name, _ in events]
        assert "error" in names

        error_event = dict(events)["error"]
        assert "RuntimeError" in error_event["message"]

    def test_error_before_any_token(self, client, buyer, sample_kb, monkeypatch):
        """检索阶段就失败的情况。"""
        async def _fail_early(question, history, kb_ids, cancel_event=None):
            raise RuntimeError("向量库连不上")
            yield  # pragma: no cover  让函数成为异步生成器

        monkeypatch.setattr(pipeline, "answer_stream", _fail_early)

        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]})
        events = parse_sse(resp.text)

        assert events[-1][0] == "error"
        assert "向量库连不上" in events[-1][1]["message"] or "RuntimeError" in events[-1][1]["message"]


# ============================================================
# 权限
# ============================================================
class TestStreamPermissions:
    def test_anonymous_rejected(self, client, sample_kb, fake_stream):
        resp = client.post(f"{API}/chat/stream", json={"question": "电池多大？"})
        assert resp.status_code == 401

    def test_cannot_stream_in_others_conversation(self, client, sample_kb, fake_stream):
        """不能往别人的会话里发流式请求。"""
        client.post(f"{API}/auth/register", json={"username": "s_a", "password": "pass123456"})
        token_a = client.post(
            f"{API}/auth/login", json={"username": "s_a", "password": "pass123456"}
        ).json()["data"]["access_token"]

        conv_id = dict(
            parse_sse(
                do_stream(
                    client,
                    {"Authorization": f"Bearer {token_a}"},
                    {"question": "电池多大？", "kb_id": sample_kb["id"]},
                ).text
            )
        )["meta"]["conversation_id"]

        client.post(f"{API}/auth/register", json={"username": "s_b", "password": "pass123456"})
        token_b = client.post(
            f"{API}/auth/login", json={"username": "s_b", "password": "pass123456"}
        ).json()["data"]["access_token"]

        resp = do_stream(
            client,
            {"Authorization": f"Bearer {token_b}"},
            {"question": "偷看", "conversation_id": conv_id},
        )

        # **必须是 404，不能是 200 + error 事件**。
        # 校验现在跑在返回流之前（见 chat_service.prepare_stream 的说明），
        # 所以能给出真正的状态码。之前它是在生成器内部抛的，
        # 那时状态码 200 已经发出去了，前端只能拿到一条 error 事件。
        assert resp.status_code == 404


class TestStreamErrorsHaveRealStatusCodes:
    """流式接口的前置校验必须返回真实的状态码。

    这是修一个真实 bug 时补的回归测试。修之前，限流、并发超限、
    会话不存在这些情况全都是：
        HTTP 200 + {"message": "生成回答时出错了：HTTPException"}
    用户看到的是这句毫无信息量的话，完全不知道该怎么办。

    根本原因：Starlette 会**先发出状态码 200，再开始迭代生成器**，
    所以写在生成器里的校验已经来不及影响状态码了。
    修法是把校验挪到生成器外面（chat_service.prepare_stream）。
    """

    def test_rate_limited_returns_429_not_200(self, client, buyer, sample_kb, fake_stream):
        """提问太频繁要返回 429，并且带上重试等待时间。"""
        # 把限流阈值调到极低，省得真发几十个请求把测试拖慢
        from app.core import rate_limit

        monkey_limiter = rate_limit.chat_limiter
        original_max = monkey_limiter.max_requests
        monkey_limiter.max_requests = 1
        try:
            # 第一次正常
            first = do_stream(
                client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]}
            )
            assert first.status_code == 200

            # 第二次超过阈值
            second = do_stream(
                client, buyer, {"question": "电池多大？", "kb_id": sample_kb["id"]}
            )
        finally:
            monkey_limiter.max_requests = original_max

        assert second.status_code == 429, (
            f"限流居然返回了 {second.status_code}——如果这里拿到 200，"
            "说明校验又被写回生成器里面了"
        )
        # 提示语要说人话，不能是 "生成回答时出错了：HTTPException"
        assert "频繁" in second.json()["message"]
        assert "Retry-After" in second.headers

    def test_unknown_conversation_returns_404(self, client, buyer, sample_kb, fake_stream):
        """往不存在的会话里发消息要返回 404。"""
        resp = do_stream(
            client, buyer, {"question": "电池多大？", "conversation_id": 999999}
        )
        assert resp.status_code == 404

    def test_unknown_kb_returns_404(self, client, buyer, fake_stream):
        """指定一个不存在的知识库要返回 404。"""
        resp = do_stream(client, buyer, {"question": "电池多大？", "kb_id": 999999})
        assert resp.status_code == 404
