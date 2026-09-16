"""对话模型工厂。

走的是百炼的 OpenAI 兼容端点 —— 也就是说，我们用的是官方维护的
`langchain-openai` 包，只是把 base_url 指向阿里云而已。
好处是流式输出、工具调用这些能力都是现成的，不用自己造。
"""

import logging
from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.core.config import settings

logger = logging.getLogger(__name__)


def _build(temperature: float, streaming: bool, max_tokens: int | None) -> ChatOpenAI:
    """创建一个对话模型实例。"""
    if not settings.DASHSCOPE_API_KEY:
        raise RuntimeError(
            "没有配置 DASHSCOPE_API_KEY，请在 backend/.env 里填写你的百炼 API Key"
        )

    return ChatOpenAI(
        model=settings.LLM_MODEL,
        api_key=settings.DASHSCOPE_API_KEY,
        base_url=settings.DASHSCOPE_BASE_URL,
        temperature=temperature,
        streaming=streaming,
        max_tokens=max_tokens,
        # 流式模式下必须显式要求返回用量统计。
        # OpenAI 兼容接口在流式响应里默认**不带** token 用量（只在最后一个
        # chunk 附带，而且还得主动要），不设置的话拿到的永远是 0，
        # 统计看板上的 token 消耗和成本估算就全是错的。
        # 实测坑：这个参数不加，流式问答的 token 数一直显示 0。
        stream_usage=True,
        # 超时给得比较宽松：网络抖动时宁可多等一会儿，也不要直接失败
        timeout=90,
        max_retries=2,
    )


@lru_cache(maxsize=4)
def get_llm(temperature: float = 0.3, streaming: bool = False) -> ChatOpenAI:
    """获取用于**生成回答**的模型。

    为什么 temperature 默认 0.3 而不是 0：完全为 0 时模型输出会很机械、
    句式重复。0.3 在保持事实准确的同时让表达自然一些。
    但也不能更高——知识库问答要的是准确复述资料，不是发挥创意。

    为什么要缓存：每创建一个实例都要初始化 HTTP 连接池，
    问答接口每个请求都新建一个会明显变慢。
    """
    return _build(temperature=temperature, streaming=streaming, max_tokens=None)


@lru_cache(maxsize=4)
def get_utility_llm(temperature: float = 0.0) -> ChatOpenAI:
    """获取用于**内部辅助任务**的模型。

    目前的用途是查询改写（把「它多少钱」改写成「XX手机 价格」）。
    这类任务要的是稳定和快，不需要创造力，所以 temperature 设为 0，
    保证同样的输入永远得到同样的输出。
    """
    return _build(temperature=temperature, streaming=False, max_tokens=256)


def reset_llm_cache() -> None:
    """清空模型缓存。改了配置（比如换了模型）之后调用，让新配置生效。"""
    get_llm.cache_clear()
    get_utility_llm.cache_clear()
