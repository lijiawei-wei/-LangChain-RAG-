"""问答编排：把用户提问、会话管理、RAG 流水线、消息持久化串起来。"""

import hashlib
import json
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import cache as answer_cache
from app.core import rate_limit
from app.core.config import settings
from app.models.document import Document, DocumentStatus
from app.models.knowledge_base import KnowledgeBase
from app.models.qa_log import QALog
from app.models.user import User
from app.rag import pipeline
from app.rag.prompts import RAG_SYSTEM_PROMPT
from app.schemas.chat import ChatRequest, ChatResponse
from app.services import conversation_service

logger = logging.getLogger(__name__)


# ============================================================
# 限流与并发控制
# ============================================================
def check_rate_limit(user_id: int) -> None:
    """检查用户是否提问过于频繁。

    问答接口的限流比普通接口更严，因为它每次都要花钱调模型。
    """
    allowed, wait_seconds = rate_limit.chat_limiter.check(f"user:{user_id}")
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"提问太频繁了，请 {wait_seconds} 秒后再试",
            headers={"Retry-After": str(wait_seconds)},
        )


class concurrency_slot:  # noqa: N801  故意用小写，让它看起来像上下文管理器
    """单用户并发问答数限制。

    用上下文管理器（with 语句）而不是手动 acquire/release，
    是为了保证**异常时也一定会释放**——手动写的话很容易漏掉 finally，
    一旦漏了，那个用户的名额就永远还不回来，之后再也不能提问了。
    """

    def __init__(self, user_id: int) -> None:
        self.key = f"user:{user_id}"
        self.acquired = False

    def __enter__(self):
        if not rate_limit.chat_concurrency.acquire(self.key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="你还有问题正在处理中，请等它回答完再问下一个",
            )
        self.acquired = True
        return self

    def __exit__(self, *exc_info):
        if self.acquired:
            rate_limit.chat_concurrency.release(self.key)
        return False


@lru_cache(maxsize=1)
def _config_fingerprint() -> str:
    """把影响检索和生成结果的配置拼成一个指纹。

    为什么要放进缓存键里：开发阶段经常调参数（换模型、改提示词、
    调检索条数）。如果不算进键里，改完参数测试半天发现没变化——
    结果是命中了旧缓存。这个坑很浪费时间，而且很难想到。
    """
    raw = json.dumps(
        {
            "retrieval_mode": settings.RETRIEVAL_MODE,
            "rerank_enabled": settings.RERANK_ENABLED,
            "rerank_top_n": settings.RERANK_TOP_N,
            "retrieve_top_k": settings.RETRIEVE_TOP_K,
            "chunk_size": settings.CHUNK_SIZE,
            "llm_model": settings.LLM_MODEL,
            # 提示词改动直接影响回答内容，取前 300 字做指纹就够了
            "prompt": RAG_SYSTEM_PROMPT[:300],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _try_cache(question: str, kb_ids: list[int], history: list[dict]) -> dict | None:
    """尝试命中答案缓存。

    **只缓存首轮提问**，带历史对话的追问一律不缓存。

    为什么：追问的答案取决于上文。用户在会话 A 里问「它支持快充吗」，
    「它」指的是手机；在会话 B 里同一个问题，「它」可能指充电器。
    如果只按问题文本缓存，B 会话会拿到 A 的答案——完全错误。
    """
    if history:
        return None

    key = answer_cache.build_answer_key(question, kb_ids, _config_fingerprint())
    return answer_cache.get_answer(key)


def _store_cache(question: str, kb_ids: list[int], history: list[dict], value: dict) -> None:
    """写入答案缓存。同样只缓存首轮提问。"""
    if history:
        return

    key = answer_cache.build_answer_key(question, kb_ids, _config_fingerprint())
    answer_cache.set_answer(key, value)


def resolve_kb_ids(db: Session, kb_id: int | None) -> list[int]:
    """确定这次问答要检索哪些知识库。

    传了具体知识库就只查那个；没传表示「全部知识库」，
    把所有库的 ID 都返回，检索时并行查、最后合并结果。
    """
    if kb_id is not None:
        kb = db.get(KnowledgeBase, kb_id)
        if kb is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在"
            )
        return [kb.id]

    all_ids = list(db.scalars(select(KnowledgeBase.id)).all())
    if not all_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="系统里还没有任何知识库，请先让管理员上传商品资料",
        )
    return all_ids


# 从文件名里去掉这些后缀，剩下的就是商品名
# （「星耀X9Pro手机产品资料.md」→「星耀X9Pro手机」）
_PRODUCT_NAME_SUFFIXES = (
    "产品资料", "参数表", "规格表", "说明书", "使用手册", "介绍", "手册", "资料",
)

# 自我介绍里最多列几个商品名。列太多会把消息撑得很长，反而不好读
_MAX_LISTED_PRODUCTS = 6


def _product_names(filenames: list[str]) -> list[str]:
    """从文件名里提取商品名。

    数据来源是文档文件名而不是别的地方——**因为不能改表结构**
    （项目用的是 create_all，没有迁移机制，加字段会让已有的数据库直接报错）。
    文件名虽然带后缀和扩展名，但去掉之后基本就是商品名，够用了。
    """
    cleaned: list[str] = []
    for filename in filenames:
        stem = Path(filename).stem
        for suffix in _PRODUCT_NAME_SUFFIXES:
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        stem = stem.strip("_- ")
        if stem and stem not in cleaned:
            cleaned.append(stem)

    # 去掉"是别人前缀"的短名字：
    # 同一个商品往往既有「XX产品资料.md」又有「XX参数表.xlsx」，
    # 去掉后缀后就会得到「XX」和「XX笔记本」两个名字，其实是同一个东西
    return [
        name
        for name in cleaned
        if not any(name != other and name in other for other in cleaned)
    ]


def build_kb_summary(db: Session, kb_ids: list[int]) -> str:
    """拼一句"知识库里收录了哪些商品"，供自我介绍使用。

    **每次提问都会调一次**，多一次数据库查询。之所以不在乎这点开销：
    整个问答流程里有两次大模型调用和一次向量检索，相比之下一次本地
    SQLite 查询（毫秒级）可以忽略。用一个查询换代码简单，是划算的。
    """
    if not kb_ids:
        return ""

    filenames = list(
        db.scalars(
            select(Document.filename)
            .where(Document.kb_id.in_(kb_ids), Document.status == DocumentStatus.READY)
            .order_by(Document.id)
        ).all()
    )
    if not filenames:
        return ""

    names = _product_names(filenames)
    total = len(filenames)

    if not names:
        return f"目前收录了 {total} 份商品资料。"

    shown = "、".join(names[:_MAX_LISTED_PRODUCTS])
    ellipsis = "……" if len(names) > _MAX_LISTED_PRODUCTS else ""
    return f"目前收录了 {total} 份商品资料，包括：{shown}{ellipsis}"


async def ask(db: Session, user: User, request: ChatRequest) -> ChatResponse:
    """处理一次问答（非流式）。

    完整流程：
      1. 确定会话（已有会话就继续用，没有就新建）
      2. 确定检索范围
      3. 取出最近的历史对话
      4. 调 RAG 流水线生成回答
      5. 把用户提问和助手回答都存进数据库
      6. 记录一条问答日志，供统计看板和效果评估使用
    """
    check_rate_limit(user.id)

    # ---------- 1. 确定会话 ----------
    if request.conversation_id is not None:
        conversation = conversation_service.get_conversation(
            db, request.conversation_id, user.id
        )
        # 会话绑了知识库就用它的，没绑就是"全部知识库"
        kb_ids = resolve_kb_ids(db, conversation.kb_id)
    else:
        kb_ids = resolve_kb_ids(db, request.kb_id)
        conversation = conversation_service.create_conversation(
            db, user.id, kb_id=request.kb_id
        )

    # ---------- 2. 历史对话（要在存本次提问之前取）----------
    history = conversation_service.get_recent_history(db, conversation.id)

    # 首次提问时用问题内容给会话命名
    conversation_service.maybe_set_title_from_question(db, conversation, request.question)

    # 先把用户提问存下来。放在回答生成之前是有意的：
    # 万一生成过程出错，用户的提问至少还在，不会丢
    user_message = conversation_service.append_message(
        db, conversation.id, "user", request.question
    )

    # ---------- 3. 查缓存 ----------
    cached = _try_cache(request.question, kb_ids, history)

    if cached is not None:
        # 命中缓存：直接用上次的答案，跳过检索和生成
        result = pipeline.ChatResult(
            answer=cached["answer"],
            citations=cached["citations"],
            retrieved_query=cached.get("retrieved_query", request.question),
            latency_ms=0,
            stage_latency={"cache": "hit"},
        )
        is_cache_hit = True
        logger.info("问答缓存命中：%r", request.question[:40])
    else:
        # ---------- 4. 生成回答 ----------
        # 用上下文管理器限制单用户并发数，异常时也会自动释放名额
        with concurrency_slot(user.id):
            result = await pipeline.answer(
                question=request.question,
                history=history,
                kb_ids=kb_ids,
                kb_summary=build_kb_summary(db, kb_ids),
            )
        is_cache_hit = False

        # 只有拿到有效引用才缓存——没引用说明可能没检索到资料，
        # 这种情况缓存下来会让用户一直看到"无法回答"
        if result.citations:
            _store_cache(
                request.question,
                kb_ids,
                history,
                {
                    "answer": result.answer,
                    "citations": result.citations,
                    "retrieved_query": result.retrieved_query,
                },
            )

    # ---------- 5. 存助手回答（含引用快照）----------
    assistant_message = conversation_service.append_message(
        db,
        conversation.id,
        "assistant",
        result.answer,
        citations=result.citations,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        latency_ms=result.latency_ms,
    )

    # ---------- 6. 记录问答日志 ----------
    _write_qa_log(db, user, conversation.kb_id, request.question, result, cache_hit=is_cache_hit)

    return ChatResponse(
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        answer=result.answer,
        citations=result.citations,
        latency_ms=result.latency_ms,
        stage_latency=result.stage_latency,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        retrieved_query=result.retrieved_query,
    )


async def prepare_stream(db: Session, user: User, request: ChatRequest) -> dict:
    """流式问答的前置工作：各项校验 + 建会话 + 存提问。

    **这个函数必须在生成器之外调用**，这一点很关键。

    背景（这是修一个真实的 bug）：以前这些校验写在 `ask_stream` 生成器里面，
    而 Starlette 会**先把 HTTP 状态码 200 发出去，再开始迭代生成器**。
    于是生成器里抛的 HTTPException 已经来不及影响状态码了，
    只能被兜底成一条 error 事件，前端看到的是：
        HTTP 200 + "生成回答时出错了：HTTPException"
    限流、并发超限、会话不存在，全都变成这句看不懂的话。
    而同样条件下非流式接口是正确返回 429 的，两条路径行为不一致。

    挪出来之后，这里抛的异常会在响应开始之前被全局异常处理器接住，
    返回真正的 429 / 404 / 400。

    返回一个字典，交给 ask_stream 继续用。
    """
    check_rate_limit(user.id)  # 抛 429

    if request.conversation_id is not None:
        conversation = conversation_service.get_conversation(
            db, request.conversation_id, user.id
        )
        kb_ids = resolve_kb_ids(db, conversation.kb_id)
    else:
        kb_ids = resolve_kb_ids(db, request.kb_id)
        conversation = conversation_service.create_conversation(
            db, user.id, kb_id=request.kb_id
        )

    history = conversation_service.get_recent_history(db, conversation.id)
    conversation_service.maybe_set_title_from_question(db, conversation, request.question)

    # 先把用户提问存下来。放在生成之前是有意的：
    # 万一生成过程出错，用户的提问至少还在，不会丢
    user_message = conversation_service.append_message(
        db, conversation.id, "user", request.question
    )

    return {
        "conversation": conversation,
        "kb_ids": kb_ids,
        "history": history,
        "user_message": user_message,
        # 寒暄类问题的自我介绍里要列出收录了哪些商品，在这里先查好。
        # 放在这个函数里而不是 ask_stream 里，是因为它已经拿到了 db 会话
        "kb_summary": build_kb_summary(db, kb_ids),
    }


async def ask_stream(
    db: Session,
    user: User,
    request: ChatRequest,
    prepared: dict,
    cancel_event=None,
):
    """流式问答。逐条产出 (事件名, 数据) 供接口层转成 SSE。

    事件顺序：
        meta     —— 会话信息（前端拿到就能建出消息气泡）
        sources  —— 引用来源列表（**先于文字到达**，前端可提前渲染右侧面板）
        token    —— 增量文本，重复多次
        done     —— 结束，带上最终统计
        error    —— 出错了（由接口层兜底产生）

    参数 prepared 是 prepare_stream() 的返回值——校验和建会话都已经在外面做完了。

    和同步版的区别只在生成方式；会话管理、历史加载、持久化的逻辑完全一致，
    这样能保证「流式和非流式给出同样的结果」。
    """
    conversation = prepared["conversation"]
    kb_ids = prepared["kb_ids"]
    history = prepared["history"]
    user_message = prepared["user_message"]

    yield "meta", {
        "conversation_id": conversation.id,
        "user_message_id": user_message.id,
        "title": conversation.title,
    }

    # ---------- 查缓存 ----------
    cached = _try_cache(request.question, kb_ids, history)
    finished: dict | None = None
    is_cache_hit = cached is not None

    if cached is not None:
        # 命中缓存：没有逐字生成的过程，把完整答案作为一次 token 推出去。
        # 前端拿到后立刻整段显示，用户感觉是"秒回"
        logger.info("流式问答缓存命中：%r", request.question[:40])

        yield "sources", {
            "citations": cached["citations"],
            "query": cached.get("retrieved_query", request.question),
        }
        yield "token", {"delta": cached["answer"]}
        finished = {
            "answer": cached["answer"],
            "citations": cached["citations"],
            "query": cached.get("retrieved_query", request.question),
            "latency_ms": 0,
            "stage_latency": {"cache": "hit"},
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }
    else:
        # ---------- 流式生成 ----------
        # 用上下文管理器限制并发，客户端中途断开时名额也会被释放
        with concurrency_slot(user.id):
            async for event_name, data in pipeline.answer_stream(
                question=request.question,
                history=history,
                kb_ids=kb_ids,
                cancel_event=cancel_event,
                kb_summary=prepared["kb_summary"],
            ):
                if event_name == "prepared":
                    # 检索完成，先把引用推出去
                    yield "sources", {
                        "citations": data["citations"],
                        "query": data["query"],
                    }
                elif event_name == "token":
                    yield "token", data
                elif event_name == "finished":
                    finished = data

        # 生成成功且有引用才缓存
        if finished is not None and finished.get("citations"):
            _store_cache(
                request.question,
                kb_ids,
                history,
                {
                    "answer": finished["answer"],
                    "citations": finished["citations"],
                    "retrieved_query": finished.get("query", request.question),
                },
            )

    if finished is None:
        # 生成被中断（客户端断开）。此时不存助手消息——
        # 存一条残缺的回答反而会让用户困惑，不如什么都不留
        logger.info("会话 %s 的生成被中断，未保存回答", conversation.id)
        return

    # ---------- 收尾：存回答、记日志 ----------
    assistant_message = conversation_service.append_message(
        db,
        conversation.id,
        "assistant",
        finished["answer"],
        citations=finished["citations"],
        prompt_tokens=finished["prompt_tokens"],
        completion_tokens=finished["completion_tokens"],
        latency_ms=finished["latency_ms"],
    )

    _write_qa_log_from_dict(
        db, user, conversation.kb_id, request.question, finished, cache_hit=is_cache_hit
    )

    yield "done", {
        "conversation_id": conversation.id,
        "assistant_message_id": assistant_message.id,
        "title": conversation.title,
        # 带上最终完整回答。前端虽然可以靠累积的 token 拼出来，
        # 但服务端给出的这份才是权威版本——万一某个 token 在传输中丢了，
        # 前端能用它兜底纠正
        "answer": finished["answer"],
        "latency_ms": finished["latency_ms"],
        "stage_latency": finished["stage_latency"],
        "prompt_tokens": finished["prompt_tokens"],
        "completion_tokens": finished["completion_tokens"],
        "citations": finished["citations"],
    }


def _write_qa_log_from_dict(
    db: Session, user: User, kb_id: int | None, question: str, data: dict, cache_hit: bool = False
) -> None:
    """流式路径写问答日志。

    和同步版的 _write_qa_log 逻辑一样，只是入参从 ChatResult 对象
    换成了字典（流式路径里数据是以字典形式在生成器之间传递的）。
    """
    try:
        citations = data.get("citations", [])
        log = QALog(
            user_id=user.id,
            kb_id=kb_id,
            question=question,
            rewritten_query=data.get("query") if data.get("query") != question else None,
            answer=data.get("answer"),
            retrieved_chunk_ids=json.dumps(
                [c["chunk_id"] for c in citations if c.get("chunk_id")], ensure_ascii=False
            ),
            rerank_scores=json.dumps(
                {str(c["index"]): c["score"] for c in citations}, ensure_ascii=False
            ),
            stage_latency=json.dumps(data.get("stage_latency", {}), ensure_ascii=False),
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            latency_ms=data.get("latency_ms", 0),
            cache_hit=cache_hit,
        )
        db.add(log)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("写问答日志失败（不影响本次回答）")


def _write_qa_log(
    db: Session, user: User, kb_id: int | None, question: str, result, cache_hit: bool = False
) -> None:
    """写一条问答日志。

    记录的东西比 messages 表更偏运维：各阶段耗时、召回了哪些分块、
    是否命中缓存等等。统计看板和 RAG 效果评估都靠它取数。

    这里出错不该影响问答本身——日志是辅助功能，
    写不进去就算了，不能让用户因为日志表的问题看不到回答。
    """
    try:
        log = QALog(
            user_id=user.id,
            kb_id=kb_id,
            question=question,
            rewritten_query=result.retrieved_query if result.retrieved_query != question else None,
            answer=result.answer,
            retrieved_chunk_ids=json.dumps(
                [c["chunk_id"] for c in result.citations if c.get("chunk_id")],
                ensure_ascii=False,
            ),
            rerank_scores=json.dumps(
                {str(c["index"]): c["score"] for c in result.citations}, ensure_ascii=False
            ),
            stage_latency=json.dumps(result.stage_latency, ensure_ascii=False),
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=result.latency_ms,
            cache_hit=cache_hit,
        )
        db.add(log)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("写问答日志失败（不影响本次回答）")
