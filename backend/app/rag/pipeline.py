"""RAG 主流水线：检索 → 重排 → 拼上下文 → 生成带引用的回答。

整个系统的核心就在这里。完整链路：

    用户提问
      ↓ ① 历史感知查询改写（把「它多少钱」变成「XX手机多少钱」）
      ↓ ② 召回（当前：向量检索单路；M6 会加上 BM25 做混合检索）
      ↓ ③ 精排（当前：按相似度截断；M6 会换成 gte-rerank-v2）
      ↓ ④ 编号化上下文（把 [1][2] 编号烘焙进文本）
      ↓ ⑤ 生成（提示词强约束：只依据资料回答、每条结论标注来源）
      ↓ ⑥ 返回回答 + 引用列表

② 和 ③ 目前是简化版（M3 先把链路跑通），M6 会把它们替换成
混合检索 + 重排。整个结构是按「策略可替换」设计的，
到时候只改 retrieve() 内部，其它环节完全不用动。
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

from langchain_core.documents import Document
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.config import settings
from app.rag import bm25, vectorstore
from app.rag.citations import (
    build_citations,
    detect_invalid_citations,
    mark_used_citations,
    parse_cited_indices,
)
from app.rag.fusion import reciprocal_rank_fusion
from app.rag.llm import get_llm
from app.rag.memory import rewrite_query
from app.rag.prompts import (
    NO_CONTEXT_REPLY,
    RAG_SYSTEM_PROMPT,
    build_smalltalk_reply,
    format_context,
)
from app.rag.smalltalk import match_smalltalk
from app.rag.reranker import rerank as rerank_documents

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    """一次问答的完整结果。"""

    answer: str
    citations: list[dict] = field(default_factory=list)
    # 实际用于检索的问题（改写过的话就和用户原问题不同）
    retrieved_query: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    # 各阶段耗时，用于性能分析
    stage_latency: dict[str, int] = field(default_factory=dict)


@dataclass
class PreparedContext:
    """检索和上下文准备的结果，生成阶段需要的东西都在这里。"""

    query: str
    citations: list[dict] = field(default_factory=list)
    context_text: str = ""
    stage_latency: dict[str, int] = field(default_factory=dict)

    # 不需要检索和生成、直接返回的答复。
    # 目前只有寒暄类问题会用到（见 rag/smalltalk.py）：这类问题知识库里
    # 必然没有答案，走完整流程只会得到一句"没找到相关资料"，体验很差。
    # 有值时 answer() / answer_stream() 会跳过模型，直接把它当作回答返回。
    direct_reply: str | None = None

    @property
    def is_empty(self) -> bool:
        """一条资料都没检索到。"""
        return not self.citations


# ============================================================
# 检索
# ============================================================
async def _vector_retrieve(kb_ids: list[int], query: str) -> list[tuple[Document, float]]:
    """向量检索这一路。

    擅长：语义相近但用词不同的情况（问"续航"能召回写着"电池能用多久"的段落）
    短板：对型号、编号这类精确字符不敏感
    """
    results = await vectorstore.search_multi(kb_ids, query, settings.RETRIEVE_TOP_K)

    # 相似度太低的丢掉。这条过滤主要防的是"完全无关"的内容混进来，
    # 阈值故意设得低（见 config.py 里的说明）
    filtered = [
        (doc, score) for doc, score in results if score >= settings.SIMILARITY_THRESHOLD
    ]

    if len(filtered) < len(results):
        logger.debug(
            "向量路相似度过滤：%d 条 → %d 条（阈值 %.2f）",
            len(results),
            len(filtered),
            settings.SIMILARITY_THRESHOLD,
        )

    return filtered


def _keyword_retrieve_sync(kb_ids: list[int], query: str) -> list[tuple[Document, float]]:
    """关键词检索这一路（同步实现，会被丢进线程池执行）。

    擅长：型号、编号、专有名词等精确匹配（"IMX890"、"67W"、"X9 Pro"）
    短板：用户换个说法就匹配不上

    多知识库时做了一步归一化：把每个库内的 BM25 分数除以该库的最高分，
    让不同库的分数大致可比。BM25 的原始分数只在同一个语料库内有相对意义，
    直接跨库比较是不严谨的——不同库的文档长度、用词分布都不一样。
    """
    all_results: list[tuple[Document, float]] = []

    for kb_id in kb_ids:
        try:
            results = bm25.search(kb_id, query, settings.RETRIEVE_TOP_K)
        except Exception as e:
            # 某个库的关键词索引建失败（比如刚被删），不该拖垮整个检索
            logger.warning("知识库 %s 的关键词检索失败，已跳过：%s", kb_id, e)
            continue

        if not results:
            continue

        if len(kb_ids) == 1:
            all_results.extend(results)
        else:
            # 单库时没必要归一化，多库时才做
            top_score = results[0][1] or 1.0
            all_results.extend((doc, score / top_score) for doc, score in results)

    all_results.sort(key=lambda pair: pair[1], reverse=True)
    return all_results[: settings.RETRIEVE_TOP_K]


async def retrieve(kb_ids: list[int], query: str) -> list[tuple[Document, float]]:
    """从知识库里召回最相关的资料。

    完整链路（可用 config.py 里的开关逐项关闭，用于做效果对比实验）：

        ① 并行双路召回
           向量路 —— Chroma 语义检索
           关键词路 —— jieba 分词 + BM25
        ② 加权 RRF 融合（把两路结果合成一个排序）
        ③ gte-rerank-v2 精排（从 20 条精选出 5 条）

    ②③ 都做了降级处理：任何一步失败都会退回上一步的结果，
    保证不会因为某个组件出问题就让整个问答不可用。
    """
    # ---------- ① 召回 ----------
    mode = settings.RETRIEVAL_MODE

    if mode == "vector":
        candidates = await _vector_retrieve(kb_ids, query)

    elif mode == "keyword":
        candidates = await asyncio.to_thread(_keyword_retrieve_sync, kb_ids, query)

    else:  # hybrid：默认，也是效果最好的
        # 两路并行发起。用 gather 而不是顺序执行：
        # 向量路要调接口算查询向量（约 200ms），关键词路是纯内存计算（几毫秒），
        # 顺序执行的话总耗时是两者相加，并行的话取决于慢的那一路
        vector_results, keyword_results = await asyncio.gather(
            _vector_retrieve(kb_ids, query),
            asyncio.to_thread(_keyword_retrieve_sync, kb_ids, query),
        )

        # 关键词路的置信门槛：结果太少说明它没找到真正匹配的内容，
        # 只是在勉强凑数，让它参与融合会把噪声带进来（见 config.py 的说明）。
        #
        # 注意这个门槛**只用来判断要不要融合**，不能拿它去覆盖兜底逻辑——
        # 如果向量路也恰好没结果，关键词路那几条就是唯一的线索了，
        # 这时候哪怕只有一条也该用上，总比告诉用户"没找到资料"要好。
        fusion_keyword = (
            keyword_results if len(keyword_results) >= settings.BM25_MIN_RESULTS else []
        )
        if keyword_results and not fusion_keyword:
            logger.debug(
                "关键词路只找到 %d 条（门槛 %d），本次不参与融合",
                len(keyword_results),
                settings.BM25_MIN_RESULTS,
            )

        if not vector_results and not keyword_results:
            return []
        if not vector_results:
            # 向量路空、只剩关键词路：说明这是精确匹配型的问题，直接用它的结果
            candidates = keyword_results
        elif not fusion_keyword:
            # 关键词路结果太少，不融合，直接用向量结果
            candidates = vector_results
        else:
            # ---------- ② RRF 融合 ----------
            candidates = reciprocal_rank_fusion(
                [vector_results, fusion_keyword],
                weights=[settings.RRF_VECTOR_WEIGHT, settings.RRF_BM25_WEIGHT],
                k=settings.RRF_K,
                top_n=settings.RETRIEVE_TOP_K,
            )

    if not candidates:
        return []

    # ---------- ③ 精排 ----------
    if not settings.RERANK_ENABLED:
        return candidates[: settings.RERANK_TOP_N]

    docs = [doc for doc, _score in candidates]
    try:
        # 重排要调外部接口，是同步阻塞的，丢进线程池别卡住事件循环
        reranked = await asyncio.to_thread(rerank_documents, query, docs, settings.RERANK_TOP_N)
    except Exception as e:
        # 重排组件内部已经做过降级了，这里的兜底是防它完全崩掉
        logger.warning("重排环节异常，使用召回结果：%s", e)
        return candidates[: settings.RERANK_TOP_N]

    # 重排后每条文档的 metadata 里带了 rerank_score，用它作为最终的相关度
    return [
        (doc, float(doc.metadata.get("rerank_score", 0.0)))
        for doc in reranked
    ]


async def prepare(
    question: str,
    history: list[dict] | None,
    kb_ids: list[int],
    kb_summary: str = "",
) -> PreparedContext:
    """准备阶段：寒暄判定 → 查询改写 → 检索 → 构建引用 → 拼上下文。

    拆成独立函数是为了让流式和非流式两条路径共用同一套准备逻辑，
    避免两边实现不一致导致「流式答得好、非流式答得差」这种诡异问题。

    kb_summary 是"知识库里有哪些商品"的一句话摘要，只在回答寒暄类问题时
    用得上（自我介绍里要列出收录了什么）。由 chat_service 那边查好传进来——
    这一层是纯 RAG 逻辑，拿不到数据库会话。
    """
    stage: dict[str, int] = {}
    total_start = time.perf_counter()

    # ---------- ⓪ 寒暄判定 ----------
    # 放在最前面是有意义的：这类问题知识库里必然没有答案，
    # 硬走一遍检索只会得到"没找到相关资料"，让用户以为系统坏了。
    # 命中就直接返回固定答复，**连查询改写都不做**——改写本身要调一次模型，
    # 对"你是谁"这种问题纯属浪费
    category = match_smalltalk(question)
    if category is not None:
        stage["smalltalk"] = 0
        stage["prepare_total"] = int((time.perf_counter() - total_start) * 1000)
        logger.info("识别为寒暄类问题（%s），直接返回固定答复", category)
        return PreparedContext(
            query=question,
            stage_latency=stage,
            direct_reply=build_smalltalk_reply(category, kb_summary),
        )

    # ---------- ① 查询改写 ----------
    t0 = time.perf_counter()
    query = await rewrite_query(question, history)
    stage["rewrite"] = int((time.perf_counter() - t0) * 1000)

    # ---------- ② 检索 ----------
    t0 = time.perf_counter()
    results = await retrieve(kb_ids, query)
    stage["retrieve"] = int((time.perf_counter() - t0) * 1000)

    # ---------- ③ 构建引用 + 拼上下文 ----------
    t0 = time.perf_counter()
    citations = build_citations(results)
    context_text = format_context(citations)
    stage["build_context"] = int((time.perf_counter() - t0) * 1000)

    stage["prepare_total"] = int((time.perf_counter() - total_start) * 1000)

    return PreparedContext(
        query=query,
        citations=citations,
        context_text=context_text,
        stage_latency=stage,
    )


# ============================================================
# 生成
# ============================================================
def _build_messages(
    question: str,
    context_text: str,
    history: list[dict] | None,
) -> list:
    """拼出送给模型的完整消息列表。

    结构：系统提示词（含参考资料） + 最近几轮历史 + 本次提问

    历史只取最近的若干轮，这是 **token 预算控制**：
    对话聊到几十轮之后，把全部历史都塞进去会让每次请求越来越慢、越来越贵，
    而且早期内容和当前问题多半已经无关了。
    """
    system_content = RAG_SYSTEM_PROMPT.format(context=context_text)
    messages: list = [SystemMessage(content=system_content)]

    if history:
        max_messages = settings.HISTORY_MAX_TURNS * 2
        for item in history[-max_messages:]:
            content = (item.get("content") or "").strip()
            if not content:
                continue
            if item.get("role") == "user":
                messages.append(HumanMessage(content=content))
            else:
                messages.append(AIMessage(content=content))

    messages.append(HumanMessage(content=question))
    return messages


async def answer(
    question: str,
    history: list[dict] | None,
    kb_ids: list[int],
    kb_summary: str = "",
) -> ChatResult:
    """非流式问答：一次性返回完整回答。

    用途：接口测试、以及后面的 RAG 效果评估脚本。
    （评估脚本要批量跑几十个问题，流式反而不好处理。）
    """
    overall_start = time.perf_counter()

    prepared = await prepare(question, history, kb_ids, kb_summary)

    # 寒暄类问题（你好、你是谁…）：直接返回固定答复，不检索也不调模型
    if prepared.direct_reply is not None:
        return ChatResult(
            answer=prepared.direct_reply,
            citations=[],
            retrieved_query=prepared.query,
            latency_ms=int((time.perf_counter() - overall_start) * 1000),
            stage_latency=prepared.stage_latency,
        )

    # 一条资料都没检索到，直接返回固定答复，不用调模型浪费钱
    if prepared.is_empty:
        return ChatResult(
            answer=NO_CONTEXT_REPLY,
            citations=[],
            retrieved_query=prepared.query,
            latency_ms=int((time.perf_counter() - overall_start) * 1000),
            stage_latency=prepared.stage_latency,
        )

    messages = _build_messages(question, prepared.context_text, history)

    t0 = time.perf_counter()
    llm = get_llm(temperature=0.3, streaming=False)
    response = await llm.ainvoke(messages)
    stage = {**prepared.stage_latency, "generate": int((time.perf_counter() - t0) * 1000)}

    usage = getattr(response, "usage_metadata", None) or {}
    answer_text = (response.content or "").strip()

    result = ChatResult(
        answer=answer_text,
        # 回答生成之后才能判断哪些引用真的被用到了，所以要在这之后标记
        citations=mark_used_citations(answer_text, prepared.citations),
        retrieved_query=prepared.query,
        prompt_tokens=usage.get("input_tokens", 0),
        completion_tokens=usage.get("output_tokens", 0),
        latency_ms=int((time.perf_counter() - overall_start) * 1000),
        stage_latency=stage,
    )

    _log_citation_quality(result)
    return result


async def answer_stream(
    question: str,
    history: list[dict] | None,
    kb_ids: list[int],
    cancel_event: asyncio.Event | None = None,
    kb_summary: str = "",
):
    """流式问答：逐块产出结果，供 SSE 推送给前端。

    产出的是 (事件名, 数据) 二元组，事件类型有：
        prepared —— 检索完成，带上引用列表（**先于任何 token 发出**）
        token    —— 一小段增量文本
        finished —— 生成结束，带上完整回答和统计信息

    为什么把 prepared 单独作为一个事件、且放在最前面：
    检索通常只要 0.2 秒，而生成要一两秒。先把这个事件推出去，
    前端就能立刻把引用来源面板渲染出来，用户看到"它已经找到资料了"，
    等待感会明显降低——哪怕文字还要等一会儿才吐出来。

    cancel_event 用于客户端断开时中断生成，避免白白跑完再丢弃。
    """
    overall_start = time.perf_counter()
    prepared = await prepare(question, history, kb_ids, kb_summary)

    yield "prepared", {
        "query": prepared.query,
        "citations": prepared.citations,
        "stage_latency": prepared.stage_latency,
        "is_empty": prepared.is_empty,
    }

    # 寒暄类问题（你好、你是谁…）：一次把整段答复推出去，不调模型。
    # 前端拿到后立刻整段显示，用户感觉是"秒回"
    if prepared.direct_reply is not None:
        yield "token", {"delta": prepared.direct_reply}
        yield "finished", {
            "answer": prepared.direct_reply,
            "citations": [],
            "query": prepared.query,
            "latency_ms": int((time.perf_counter() - overall_start) * 1000),
            "stage_latency": prepared.stage_latency,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        return

    # 一条资料都没检索到：直接给出固定答复，不用调模型
    if prepared.is_empty:
        yield "token", {"delta": NO_CONTEXT_REPLY}
        yield "finished", {
            "answer": NO_CONTEXT_REPLY,
            "citations": [],
            "query": prepared.query,
            "latency_ms": int((time.perf_counter() - overall_start) * 1000),
            "stage_latency": prepared.stage_latency,
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
        return

    messages = _build_messages(question, prepared.context_text, history)
    llm = get_llm(temperature=0.3, streaming=True)

    t0 = time.perf_counter()
    pieces: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    # 首字延迟：从收到问题到吐出第一个字的耗时。
    # **这是流式输出最该被测量的指标**，因为它直接等于"用户干等了多久"。
    # 实测数据：同一个长回答，非流式要干等 13.8 秒，流式 0.3 秒就开始出字——
    # 总耗时其实差不多，但用户的等待感差了 98%。
    # 只测总耗时的话，这个优化在数字上完全看不出来，所以必须单独打点。
    first_token_ms: int | None = None

    # astream 是 LangChain 的异步流式接口，逐个产出 AIMessageChunk。
    # 注意必须用异步版本（astream）：同步版 stream 会阻塞事件循环，
    # 导致同进程里其它用户的请求全部排队等待
    async for chunk in llm.astream(messages):
        if cancel_event is not None and cancel_event.is_set():
            logger.info("客户端已断开，提前结束生成")
            break

        delta = chunk.content or ""
        if delta:
            # 只记第一个非空片段。注意不能放在循环外面用别的条件判断——
            # 模型开头可能先吐几个空字符串的 chunk，那些不算"用户看到字了"
            if first_token_ms is None:
                first_token_ms = int((time.perf_counter() - overall_start) * 1000)
            pieces.append(delta)
            yield "token", {"delta": delta}

        # 用量信息通常只挂在最后一个 chunk 上
        usage = getattr(chunk, "usage_metadata", None)
        if usage:
            prompt_tokens = usage.get("input_tokens", prompt_tokens)
            completion_tokens = usage.get("output_tokens", completion_tokens)

    answer_text = "".join(pieces).strip()
    stage = {**prepared.stage_latency, "generate": int((time.perf_counter() - t0) * 1000)}
    if first_token_ms is not None:
        stage["first_token"] = first_token_ms

    result = ChatResult(
        answer=answer_text,
        citations=mark_used_citations(answer_text, prepared.citations),
        retrieved_query=prepared.query,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=int((time.perf_counter() - overall_start) * 1000),
        stage_latency=stage,
    )
    _log_citation_quality(result)

    yield "finished", {
        "answer": result.answer,
        "citations": result.citations,
        "query": result.retrieved_query,
        "latency_ms": result.latency_ms,
        "stage_latency": result.stage_latency,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
    }


def _log_citation_quality(result: ChatResult) -> None:
    """检查回答的引用标注质量并记日志。

    两种情况值得关注：
      1. 模型标了不存在的编号（比如只有 3 条资料却标 [5]）——在编造引用
      2. 完全没有标注——可能是不听话，也可能是问题本身不需要资料
    这些数据将来可以用来分析提示词效果。
    """
    if not result.citations:
        return

    invalid = detect_invalid_citations(result.answer, len(result.citations))
    if invalid:
        logger.warning(
            "回答引用了不存在的编号 %s（实际只有 %d 条资料），可能存在编造引用",
            invalid,
            len(result.citations),
        )

    used = parse_cited_indices(result.answer, max_index=len(result.citations))
    if not used:
        logger.info("回答没有标注任何引用编号（检索到 %d 条资料）", len(result.citations))
