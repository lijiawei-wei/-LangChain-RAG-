"""问答日志表。

每完成一次问答就写一行，是「管理员统计看板」和「RAG 效果评估模块」的数据源。
这里记录的信息比 messages 表更偏运维/调优：各阶段耗时、命中哪些分块、
各分块的重排得分、是否命中缓存。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.utils.time import utcnow


class QALog(Base):
    __tablename__ = "qa_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    kb_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    question: Mapped[str] = mapped_column(Text, nullable=False)
    # 若开启了查询改写，这里存改写后的实际检索问题，便于对比分析改写效果
    rewritten_query: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 召回并最终入选的分块 ID 列表（JSON 数组字符串）
    retrieved_chunk_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 各分块的重排得分（JSON 对象字符串），用于分析重排效果
    rerank_scores: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 各阶段耗时（毫秒）JSON，形如
    # {"rewrite":120,"retrieve":340,"rerank":210,"generate":1800}
    # 有了分阶段打点，才能定位性能瓶颈到底在哪一环
    stage_latency: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 是否命中答案缓存。缓存命中率是看板上的核心指标之一
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, index=True, nullable=False
    )

    def __repr__(self) -> str:
        return f"<QALog id={self.id} latency={self.latency_ms}ms cache_hit={self.cache_hit}>"
