"""会话表。

一个用户可以有多个会话，每个会话是一条独立的对话线程。
用户之间严格隔离：所有查询都必须带上 user_id 过滤条件。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.utils.time import utcnow


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # 这个会话绑定哪个知识库。为空表示「全部知识库」都检索。
    kb_id: Mapped[int | None] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"), nullable=True
    )

    title: Mapped[str] = mapped_column(String(200), default="新对话", nullable=False)

    # 冗余消息数，会话列表页直接读，不用每条会话都去 count 一次消息表
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    # 每次有新消息就更新，会话列表按它倒序排（最近聊的排最上面）
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    user: Mapped["User"] = relationship(back_populates="conversations")  # noqa: F821
    messages: Mapped[list["Message"]] = relationship(  # noqa: F821
        back_populates="conversation", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # 会话列表页的固定查询是「某用户的会话，按更新时间倒序」，
        # 建这个联合索引可以让 SQLite 直接走索引取数，不用全表扫描再排序。
        Index("ix_conv_user_updated", "user_id", "updated_at"),
    )

    def __repr__(self) -> str:
        return f"<Conversation id={self.id} user_id={self.user_id} title={self.title!r}>"
