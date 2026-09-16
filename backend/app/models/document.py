"""文档表。

注意：这里只存「文档级」信息，分块的正文和向量都存在 Chroma 里。
再存一份到 SQL 属于重复存储，且两边容易不一致。
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.utils.time import utcnow


class DocumentStatus:
    """文档处理状态机的取值。

    处理是异步后台跑的多步骤流程，前端靠轮询这个状态显示进度。
    """

    PENDING = "pending"  # 已记录，排队等待处理
    PARSING = "parsing"  # 正在解析文件内容
    CHUNKING = "chunking"  # 正在切分文本块
    EMBEDDING = "embedding"  # 正在向量化并写入向量库
    READY = "ready"  # 处理完成，可被检索
    FAILED = "failed"  # 处理失败，error_msg 里是原因

    # 供前端判断「是否还在处理中」，决定要不要继续轮询
    IN_PROGRESS = (PENDING, PARSING, CHUNKING, EMBEDDING)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    kb_id: Mapped[int] = mapped_column(
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 文件落盘后的实际路径（原名可能重名，落盘时会加 uuid 前缀）
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)

    # 文件内容的 SHA-256，用于实现「上传幂等」：
    # 同一个文件重复上传时直接返回已有记录，不会重复解析和重复计费向量化。
    #
    # 唯一性约束是 **（知识库 + 文件内容）** 的组合，不是全局唯一的。
    # 为什么不做成全局唯一：那样的话，同一份资料只能存在于一个知识库里，
    # 想把它也放进另一个知识库时会被拒绝，提示还说的是"内容与已有文档重复"——
    # 可用户在当前知识库里根本找不到那份文档，完全摸不着头脑。
    # 商品资料被多个知识库共用是很常见的需求，所以按知识库各自去重。
    file_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), default=DocumentStatus.PENDING, index=True, nullable=False
    )
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)

    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, index=True, nullable=False
    )
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    knowledge_base: Mapped["KnowledgeBase"] = relationship(  # noqa: F821
        back_populates="documents"
    )

    __table_args__ = (
        # 同一个知识库里不允许出现两份内容相同的文档；
        # 但不同知识库里可以有（同一份商品资料被多个库共用是正常需求）
        UniqueConstraint("kb_id", "file_hash", name="uq_document_kb_hash"),
    )

    def __repr__(self) -> str:
        return f"<Document id={self.id} filename={self.filename!r} status={self.status}>"
