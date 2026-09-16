"""会话与消息的业务逻辑。

**这个文件里最重要的一件事是数据隔离**：每个会话都属于某个用户，
查询、修改、删除时都必须校验归属，绝不能出现"A 用户能看到 B 用户会话"的情况。
"""

import json
import logging

from fastapi import HTTPException, status
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.message import Message, MessageRole
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# 自动生成的会话标题最多取多少个字
AUTO_TITLE_LENGTH = 24


def get_conversation(db: Session, conversation_id: int, user_id: int) -> Conversation:
    """按 ID 取会话，同时校验它属不属于这个用户。

    **这是数据隔离的关键函数**，所有涉及具体会话的操作都必须先过它。

    注意越权时返回的是 404 而不是 403：403 的意思等于告诉对方
    「这个会话确实存在，只是不属于你」，攻击者可以拿它来探测系统里
    到底有多少会话、ID 都长什么样。返回 404 则什么信息都不泄露。
    """
    conversation = db.get(Conversation, conversation_id)

    if conversation is None or conversation.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="会话不存在"
        )

    return conversation


def list_conversations(
    db: Session,
    user_id: int,
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
) -> tuple[list[Conversation], int]:
    """分页查询某个用户的会话列表。

    注意 user_id 是必传的过滤条件——这就是"每个用户有每个用户的独立会话"
    在代码层面的落实。
    """
    stmt = select(Conversation).where(Conversation.user_id == user_id)

    if keyword:
        stmt = stmt.where(Conversation.title.ilike(f"%{keyword}%"))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    stmt = (
        # 最近聊过的排最前面
        stmt.order_by(Conversation.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(db.scalars(stmt).all()), total


def create_conversation(
    db: Session,
    user_id: int,
    kb_id: int | None = None,
    title: str | None = None,
) -> Conversation:
    """新建会话。"""
    conversation = Conversation(
        user_id=user_id,
        kb_id=kb_id,
        title=title or "新对话",
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation


def update_conversation(
    db: Session,
    conversation_id: int,
    user_id: int,
    title: str | None = None,
    kb_id: int | None = None,
) -> Conversation:
    """重命名会话，或切换它绑定的知识库。"""
    conversation = get_conversation(db, conversation_id, user_id)

    if title is not None:
        conversation.title = title
    if kb_id is not None:
        conversation.kb_id = kb_id

    db.commit()
    db.refresh(conversation)
    return conversation


def delete_conversation(db: Session, conversation_id: int, user_id: int) -> None:
    """删除会话。它下面的消息会因为级联删除一并消失。"""
    conversation = get_conversation(db, conversation_id, user_id)
    db.delete(conversation)
    db.commit()
    logger.info("用户 %s 删除了会话 %s", user_id, conversation_id)


def clear_conversations(db: Session, user_id: int) -> int:
    """清空某个用户的全部会话。返回删除了多少个。"""
    result = db.execute(delete(Conversation).where(Conversation.user_id == user_id))
    db.commit()
    count = result.rowcount or 0
    logger.info("用户 %s 清空了 %d 个会话", user_id, count)
    return count


# ============================================================
# 消息
# ============================================================
def list_messages(
    db: Session,
    conversation_id: int,
    user_id: int,
    limit: int = 50,
    before_id: int | None = None,
) -> tuple[list[Message], int]:
    """分页拉取会话里的历史消息。

    用「游标分页」（传 before_id 取更早的消息）而不是页码分页：
    聊天记录是不断增长的，用页码分页时新消息一进来，"第 2 页"的内容
    就往后错位了，用户往上翻会看到重复消息。游标分页没有这个问题。
    """
    get_conversation(db, conversation_id, user_id)  # 先校验归属

    stmt = select(Message).where(Message.conversation_id == conversation_id)

    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)

    total = db.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
    ) or 0

    # 取最近 limit 条。数据库里是正序存的，这里倒序取再翻转回来
    stmt = stmt.order_by(Message.id.desc()).limit(limit)
    messages = list(db.scalars(stmt).all())
    messages.reverse()

    return messages, total


def get_recent_history(db: Session, conversation_id: int, max_messages: int = 20) -> list[dict]:
    """取最近几轮对话，供查询改写和上下文拼装使用。

    只取最近若干条：更早的内容和当前问题多半已经无关，
    全部带上既浪费 token 又会干扰模型判断。
    """
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.id.desc())
        .limit(max_messages)
    )
    messages = list(db.scalars(stmt).all())
    messages.reverse()

    return [
        {"role": m.role, "content": m.content}
        for m in messages
        if m.content and m.content.strip()
    ]


def append_message(
    db: Session,
    conversation_id: int,
    role: str,
    content: str,
    citations: list[dict] | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
) -> Message:
    """往会话里追加一条消息。"""
    message = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        # 引用以 JSON 字符串存库。存快照而不是只存 ID，
        # 这样即使原文档后来被删，历史对话里的引用依然能完整展示
        citations=json.dumps(citations, ensure_ascii=False) if citations else None,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        latency_ms=latency_ms,
    )
    db.add(message)

    # 同步维护会话的冗余字段：消息数用于列表展示，更新时间用于排序
    conversation = db.get(Conversation, conversation_id)
    if conversation is not None:
        conversation.message_count += 1
        conversation.updated_at = utcnow()

    db.commit()
    db.refresh(message)
    return message


def maybe_set_title_from_question(db: Session, conversation: Conversation, question: str) -> None:
    """如果会话还是默认标题，就用第一个问题给它起个名。

    为什么需要：如果所有会话都叫「新对话」，用户在左侧列表里根本分不清
    哪个是哪个，得一个个点开看。用第一条提问当标题，一眼就能认出来。
    """
    if conversation.title and conversation.title != "新对话":
        return

    title = question.strip().replace("\n", " ")
    if len(title) > AUTO_TITLE_LENGTH:
        title = title[:AUTO_TITLE_LENGTH] + "…"

    conversation.title = title or "新对话"
    db.commit()
