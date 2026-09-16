"""外部接口调用的超时配置测试。

**为什么值得单独测这个**：超时没设对，功能上完全看不出来——
接口能调通、结果也对，只有网络出问题的时候才会暴露。
而暴露出来的表现极具迷惑性：一次简单的检索要跑好几分钟，
界面像卡死了，日志里却没有任何报错（因为请求确实还在"正常"进行中）。

实际踩到的就是这个问题：DashScope SDK 的默认超时是 **300 秒**，
配上 4 次重试，单次向量化最长能卡 20 分钟。
实测一次检索因为 DNS 抖动耗时 197 秒，从头到尾没有任何错误提示。

所以这里用假的接口调用把超时参数"抓"下来验证，
这样测试跑得飞快、也不花钱。
"""

import pytest

from app.rag.embeddings import DashScopeEmbeddings
from app.rag.reranker import DashScopeReranker

# SDK 的默认超时。写在这里是为了让断言有对照，
# 一眼能看出我们传的值和默认值差了多远
SDK_DEFAULT_TIMEOUT_SECONDS = 300


class _FakeResponse:
    """假的接口返回值，够用就行。"""

    def __init__(self, status_code=200, output=None):
        self.status_code = status_code
        self.output = output if output is not None else {}
        self.message = ""


class TestEmbeddingTimeout:
    def test_request_timeout_is_passed(self, monkeypatch):
        """调用向量化接口时必须显式带上超时参数。"""
        captured = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return _FakeResponse(
                output={"embeddings": [{"text_index": 0, "embedding": [0.1] * 8}]}
            )

        monkeypatch.setattr("app.rag.embeddings.dashscope.TextEmbedding.call", fake_call)

        embedding = DashScopeEmbeddings(api_key="fake-key")
        embedding._call_api(["测试文本"])

        assert "request_timeout" in captured, (
            "没有传 request_timeout！SDK 会用它 300 秒的默认值，"
            "网络一抖动整个问答会卡好几分钟"
        )
        assert captured["request_timeout"] < SDK_DEFAULT_TIMEOUT_SECONDS

    def test_default_timeout_is_reasonable(self):
        """默认超时要在"够用"和"别太久"之间。

        太短：网络慢一点的请求会被掐断，然后重试、再掐断……
              本来 25 秒能成功的请求变成 45 秒还在转圈，**比不设超时还糟**。
              这是实测踩到的：一次提问的向量化被调用了 3 次。
        太长：真断网时用户要干等很久，这正是要修的问题（SDK 默认 300 秒）。

        所以下限卡在 30 秒：网络抖动实测能慢到 20~30 秒，
        低于这个值就会开始误伤正常请求。
        """
        embedding = DashScopeEmbeddings(api_key="fake-key")
        assert 30 <= embedding.request_timeout <= 120
        assert embedding.request_timeout < SDK_DEFAULT_TIMEOUT_SECONDS

    def test_timeout_is_configurable(self):
        """允许调整，方便按实际情况改。"""
        embedding = DashScopeEmbeddings(api_key="fake-key", request_timeout=45)
        assert embedding.request_timeout == 45


class TestRerankTimeout:
    def test_request_timeout_is_passed(self, monkeypatch):
        """重排接口同样必须带超时。"""
        captured = {}

        def fake_call(**kwargs):
            captured.update(kwargs)
            return _FakeResponse(
                output={"results": [{"index": 0, "relevance_score": 0.5}]}
            )

        monkeypatch.setattr("app.rag.reranker.dashscope.TextReRank.call", fake_call)

        reranker = DashScopeReranker(api_key="fake-key")
        reranker._call_api("问题", ["文档一", "文档二"], top_n=2)

        assert "request_timeout" in captured, (
            "没有传 request_timeout！重排正常只要几百毫秒，"
            "却可能因为默认超时卡住 5 分钟"
        )
        assert captured["request_timeout"] < SDK_DEFAULT_TIMEOUT_SECONDS


class TestLLMTimeout:
    def test_chat_model_has_timeout_and_bounded_retries(self):
        """对话模型也要设超时，而且重试次数不能是无限的。

        如果不限次数，遇到持续故障就会一直重试下去，
        用户那边永远等不到失败提示，只能看着界面转圈。
        """
        from app.rag.llm import get_llm

        llm = get_llm()
        assert llm.request_timeout is not None
        assert llm.max_retries is not None
        assert llm.max_retries <= 3
