"""消息表。

每条用户提问和助手回答都存一行，这是「跨登录恢复历史对话」的数据基础。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.utils.time import utcnow


class MessageRole:
    USER = "user"
    ASSISTANT = "assistant"


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), index=True, nullable=False
    )

    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 引用来源快照，存 JSON 字符串。结构：
    # [{"index":1, "doc_id":3, "filename":"xx.pdf", "page":2,
    #   "score":0.87, "chunk_id":"...", "text":"原文截断500字"}]
    #
    # 为什么存快照而不是只存 chunk_id：如果只存 ID，等管理员把那份文档删了，
    # 用户回看历史对话时引用就全部落空、点开是空白。存快照后即使原文档已删除，
    # 历史对话里的引用依然能完整展示。
    citations: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 用量与性能指标，用于统计看板
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    conversation: Mapped["Conversation"] = relationship(  # noqa: F821
        back_populates="messages"
    )

    __table_args__ = (
        # 拉取某会话的历史消息固定是「按时间正序、分页取」，
        # 联合索引让排序和翻页都能走索引。
        Index("ix_msg_conv_created", "conversation_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<Message id={self.id} conv={self.conversation_id} role={self.role}>"
