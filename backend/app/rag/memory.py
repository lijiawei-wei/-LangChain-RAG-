"""历史感知的查询改写。

解决的问题：多轮对话里用户会用指代词。

    用户：星耀 X9 Pro 的电池容量是多少？
    助手：星耀 X9 Pro 内置 5000mAh 电池 [1]。
    用户：它支持快充吗？          ← "它"指什么？

如果没有改写，「它支持快充吗」这句话直接拿去检索向量库，
和商品资料里的文字匹配度极低，很可能一条相关资料都召回不到，
用户就会看到"根据现有资料无法回答"——明明资料里有这个信息。

改写之后变成「星耀 X9 Pro 支持快充吗」，检索就能正常命中了。
"""

import logging

from langchain_core.messages import HumanMessage, SystemMessage

from app.core.config import settings
from app.rag.llm import get_utility_llm
from app.rag.prompts import QUERY_REWRITE_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# 送去改写的最大历史轮数。太多没必要——指代词一般只跟最近一两轮有关，
# 而每多一轮就多花一点 token
MAX_HISTORY_TURNS_FOR_REWRITE = 3


def _format_history(history: list[dict]) -> str:
    """把历史对话格式化成纯文本，供提示词使用。"""
    recent = history[-MAX_HISTORY_TURNS_FOR_REWRITE * 2 :] if history else []

    lines = []
    for message in recent:
        role = "用户" if message.get("role") == "user" else "助手"
        content = (message.get("content") or "").strip()
        if content:
            # 单条太长就截断，改写只需要知道"在聊什么商品"，不需要完整回答
            lines.append(f"{role}：{content[:300]}")

    return "\n".join(lines) if lines else "（这是对话的开始，没有历史）"


async def rewrite_query(question: str, history: list[dict] | None) -> str:
    """把带指代词的问题改写成独立完整的问题。

    几种直接返回原问题的情形（都不该浪费一次模型调用）：
      - 功能被配置关掉了
      - 这是第一轮对话，没有历史可参考
      - 问题里明显没有指代词

    改写失败时也返回原问题——这个环节是锦上添花，
    不能因为它出错就让整个问答挂掉。
    """
    if not settings.QUERY_REWRITE_ENABLED:
        return question

    if not history:
        return question

    # 没有指代词的迹象就不必改写，省一次调用也省一点延迟
    pronouns = ("它", "他", "她", "这个", "那个", "这款", "那款", "该", "此", "这", "那")
    if not any(p in question for p in pronouns):
        return question

    # 问题本身已经很长，多半说清楚了，改写反而可能画蛇添足
    if len(question) > 60:
        return question

    try:
        llm = get_utility_llm()
        messages = [
            SystemMessage(
                content=QUERY_REWRITE_SYSTEM_PROMPT.format(
                    history=_format_history(history), question=question
                )
            ),
            HumanMessage(content="请输出改写后的问题。"),
        ]

        response = await llm.ainvoke(messages)
        rewritten = (response.content or "").strip()

        # 兜底校验：改写结果太离谱就放弃
        if not rewritten or len(rewritten) > 200:
            return question

        # 去掉模型偶尔会加上的引号
        rewritten = rewritten.strip('"\'“”‘’')

        if rewritten != question:
            logger.info("查询改写：%r → %r", question, rewritten)

        return rewritten

    except Exception as e:
        # 网络抖动、模型限流都有可能。改写只是优化，失败不影响主流程
        logger.warning("查询改写失败，使用原问题：%s", e)
        return question
