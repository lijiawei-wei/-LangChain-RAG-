"""知识库表。

设计为「多知识库」：管理员可以按商品类目建多个库（如「手机数码」「家用电器」），
每个知识库在 Chroma 里对应一个独立的 collection，彼此向量互不干扰。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.utils.time import utcnow


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # 冗余统计字段。列表页要显示文档数/分块数，如果每次都去 count 一遍
    # documents 表和查 Chroma，列表页会很慢。这里冗余存储，在增删文档时维护。
    doc_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )

    documents: Mapped[list["Document"]] = relationship(  # noqa: F821
        back_populates="knowledge_base", cascade="all, delete-orphan"
    )

    @property
    def collection_name(self) -> str:
        """对应的 Chroma collection 名。

        用固定前缀 + 主键，保证一个知识库一个 collection，删除知识库时
        直接 drop 掉整个 collection 即可，不会误删其它库的数据。
        """
        return f"kb_{self.id}"

    def __repr__(self) -> str:
        return f"<KnowledgeBase id={self.id} name={self.name!r}>"
