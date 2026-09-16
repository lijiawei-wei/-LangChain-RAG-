"""回答反馈表（点赞/点踩）。

作用有两个：
1. 给管理员看哪些回答质量差，便于发现知识库盲区；
2. 作为论文里「回答质量人工评估」数据来源。
"""

from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # 1 = 点赞，-1 = 点踩
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )

    __table_args__ = (
        # 同一个用户对同一条消息只能有一条反馈：
        # 重复点踩时应该是「更新」而不是「插入第二行」，否则统计会翻倍。
        UniqueConstraint("message_id", "user_id", name="uq_feedback_msg_user"),
    )

    def __repr__(self) -> str:
        return f"<Feedback msg={self.message_id} rating={self.rating}>"
