"""统计看板的数据汇总。

数据来源全部是现有的表，不额外建统计表——**每次实时算**。
为什么不做定时汇总表：汇总表要处理"什么时候更新、漏更新了怎么办、
历史数据改了要不要回补"一堆问题，一旦不一致极难排查。
本项目的数据量（一个毕设演示系统）实时算完全够快，
用简单换正确，这个取舍是划算的。
"""

import json
import logging
from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.config import settings
from app.models.conversation import Conversation
from app.models.document import Document, DocumentStatus
from app.models.feedback import Feedback
from app.models.knowledge_base import KnowledgeBase
from app.models.message import Message, MessageRole
from app.models.qa_log import QALog
from app.models.user import User

logger = logging.getLogger(__name__)

# 算"平均首字延迟"时最多回溯多少条日志。
# 首字延迟存在 stage_latency 这个 JSON 字段里（见下文的说明），
# 解析 JSON 只能在 Python 里做，所以要限制读取量，避免日志涨到几万条之后拖慢看板。
FIRST_TOKEN_SAMPLE_SIZE = 500


# ============================================================
# 时区换算
# ============================================================
# 数据库里存的是 UTC 时间，但看板上的"今天""昨天"必须按用户所在时区算。
# 不做换算的话，北京时间早上 7 点提的问题会被统计成"昨天"的，
# 而"今日问答"要等到早上 8 点才刷新——数字看着就是不对的。
_LOCAL_OFFSET = timedelta(hours=settings.STATS_TIMEZONE_OFFSET_HOURS)


def _to_local_date(dt_utc: datetime) -> date:
    """把库里的 UTC 时间换算成本地日期。"""
    return (dt_utc + _LOCAL_OFFSET).date()


def _local_day_start_utc(day: date) -> datetime:
    """某个本地日期的零点，对应到 UTC 是几点。

    用于查"今天的数据"：本地今天 00:00 相当于 UTC 的前一天 16:00。
    """
    return datetime.combine(day, time.min) - _LOCAL_OFFSET


def _today_local() -> date:
    return _to_local_date(datetime.now(timezone.utc).replace(tzinfo=None))


def _percent(part: int, total: int) -> float:
    """算百分比，自动处理除零。

    空表时返回 0.0 而不是抛 ZeroDivisionError——新装好的系统什么数据都没有，
    看板必须能正常打开，这是最基本的要求。
    """
    if not total:
        return 0.0
    return round(part / total * 100, 1)


def _rate(part: int, total: int) -> float:
    """算比例（0~1），保留三位小数。同样处理除零。"""
    if not total:
        return 0.0
    return round(part / total, 3)


# ============================================================
# 总览
# ============================================================
def get_overview(db: Session) -> dict:
    """看板顶部那排数字卡片的数据。"""
    # ---------- 用户 ----------
    total_users = db.scalar(select(func.count()).select_from(User)) or 0
    active_users = (
        db.scalar(
            select(func.count()).select_from(User).where(User.is_active.is_(True))
        )
        or 0
    )

    # ---------- 知识库与文档 ----------
    kb_count = db.scalar(select(func.count()).select_from(KnowledgeBase)) or 0
    doc_total = db.scalar(select(func.count()).select_from(Document)) or 0
    doc_ready = (
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.status == DocumentStatus.READY)
        )
        or 0
    )
    doc_failed = (
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.status == DocumentStatus.FAILED)
        )
        or 0
    )
    # 分块总数直接从知识库表上的冗余字段加总（由 refresh_stats 维护），
    # 比去向量库一条条数快得多
    chunk_count = (
        db.scalar(select(func.coalesce(func.sum(KnowledgeBase.chunk_count), 0))) or 0
    )

    # ---------- 问答 ----------
    qa_total = db.scalar(select(func.count()).select_from(QALog)) or 0
    qa_cache_hits = (
        db.scalar(
            select(func.count()).select_from(QALog).where(QALog.cache_hit.is_(True))
        )
        or 0
    )

    today_start_utc = _local_day_start_utc(_today_local())
    qa_today = (
        db.scalar(
            select(func.count())
            .select_from(QALog)
            .where(QALog.created_at >= today_start_utc)
        )
        or 0
    )

    # 平均响应延迟**只统计未命中缓存的问答**。
    #
    # 为什么排除缓存命中：缓存命中是直接返回的，延迟按定义就是 0 毫秒。
    # 把它算进平均值，会让数字随缓存命中率一起漂移——
    # 命中率越高，"平均延迟"看起来越小，但它根本不反映真实的生成耗时。
    # 想看缓存的效果应该看"缓存命中率"那个独立指标，两者不要混在一起。
    real_qa_filter = QALog.cache_hit.is_(False)
    avg_latency = db.scalar(
        select(func.avg(QALog.latency_ms)).where(real_qa_filter)
    )
    prompt_tokens = (
        db.scalar(select(func.coalesce(func.sum(QALog.prompt_tokens), 0))) or 0
    )
    completion_tokens = (
        db.scalar(select(func.coalesce(func.sum(QALog.completion_tokens), 0))) or 0
    )

    # ---------- 反馈 ----------
    fb_total = db.scalar(select(func.count()).select_from(Feedback)) or 0
    fb_likes = (
        db.scalar(
            select(func.count()).select_from(Feedback).where(Feedback.rating == 1)
        )
        or 0
    )
    fb_dislikes = (
        db.scalar(
            select(func.count()).select_from(Feedback).where(Feedback.rating == -1)
        )
        or 0
    )

    return {
        "users": {"total": total_users, "active": active_users},
        "knowledge_bases": kb_count,
        "documents": {
            "total": doc_total,
            "ready": doc_ready,
            "failed": doc_failed,
        },
        "chunks": int(chunk_count),
        "qa": {
            "total": qa_total,
            "today": qa_today,
            "cache_hits": qa_cache_hits,
            "cache_hit_rate": _percent(qa_cache_hits, qa_total),
            "avg_latency_ms": int(avg_latency) if avg_latency is not None else 0,
            "avg_first_token_ms": get_avg_first_token_ms(db),
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": int(prompt_tokens) + int(completion_tokens),
        },
        "feedback": {
            "total": fb_total,
            "likes": fb_likes,
            "dislikes": fb_dislikes,
            # 满意度 = 赞 / (赞 + 踩)。没有任何反馈时返回 0，
            # 前端那边要显示成"—"而不是"0%"，否则会误导成"用户全都不满意"
            "satisfaction": _rate(fb_likes, fb_likes + fb_dislikes),
        },
    }


def get_avg_first_token_ms(db: Session) -> int:
    """平均首字延迟（毫秒）——用户从提问到看见第一个字，平均要等多久。

    **为什么这个指标值得单独算**：网页问答里用户最在意的是"多久有反应"，
    而不是"总共花了多久"。实测同一段长回答，非流式要干等 13.8 秒，
    流式 0.3 秒就开始出字，总耗时其实差不多——光看平均延迟，
    这个优化在数字上完全体现不出来，必须靠首字延迟才能说明白。

    **实现上的一个取舍**：这个值存在 qa_logs.stage_latency 这个 JSON 字段里，
    而不是单独的列。原因是我们**不能改表结构**（项目用的是 create_all，
    没有迁移机制，加字段会让已有的数据库直接报错）。代价是取数时要解析 JSON，
    只能在 Python 里做、也没法用 SQL 聚合，所以只回溯最近若干条日志。
    数据量再大一个量级的话，应该改成独立的列 + 一次正式的数据库迁移。
    """
    rows = db.scalars(
        select(QALog.stage_latency)
        .where(QALog.stage_latency.is_not(None))
        .order_by(QALog.id.desc())
        .limit(FIRST_TOKEN_SAMPLE_SIZE)
    ).all()

    values: list[int] = []
    for raw in rows:
        try:
            value = json.loads(raw).get("first_token")
        except (json.JSONDecodeError, AttributeError, TypeError):
            # 老数据里没有这个字段，或者 JSON 坏了。跳过就好，
            # 不能因为一条脏数据让整个看板打不开
            continue
        if isinstance(value, int) and value >= 0:
            values.append(value)

    return int(sum(values) / len(values)) if values else 0


# ============================================================
# 趋势
# ============================================================
def get_trend(db: Session, days: int = 7) -> list[dict]:
    """最近 N 天每天的问答量、token 消耗、平均延迟、缓存命中数。

    **按天分组是在 Python 里做的，不是 SQL**。两个原因：
      1. 时区：库里存 UTC，按 UTC 日期分组会让"今天"从早上 8 点才开始，
         必须换算成本地日期再分桶
      2. 兼容性：SQLite 用 date() 函数、PostgreSQL 得用 CAST(... AS DATE)，
         写哪个都会破坏"代码可切换数据库"的约定。在 Python 里分桶两边都不用管

    代价是要把窗口内的行读进内存。所以这里**只 select 需要的几列**，
    而不是整行 ORM 对象——数据量变大时差别很明显。
    """
    days = max(1, min(days, 90))
    today = _today_local()
    start_day = today - timedelta(days=days - 1)
    start_utc = _local_day_start_utc(start_day)

    rows = db.execute(
        select(
            QALog.created_at,
            QALog.prompt_tokens,
            QALog.completion_tokens,
            QALog.latency_ms,
            QALog.cache_hit,
        ).where(QALog.created_at >= start_utc)
    ).all()

    # 先建好所有日期并填 0。**这一步不能省**：
    # 如果某天没人提问，SQL 分组结果里就根本没有这一天，
    # 直接拿去画折线图会跳过它，看起来像时间轴被压缩了。
    buckets: dict[date, dict] = {}
    for offset in range(days):
        day = start_day + timedelta(days=offset)
        buckets[day] = {
            "date": day.isoformat(),
            "questions": 0,
            "tokens": 0,
            "cache_hits": 0,
            # 延迟要单独累加再除以"真实生成的次数"，
            # 不能把缓存命中的 0 一起平均进去（理由同 get_overview）
            "_latency_sum": 0,
            "_latency_count": 0,
        }

    for created_at, prompt_tokens, completion_tokens, latency_ms, cache_hit in rows:
        bucket = buckets.get(_to_local_date(created_at))
        if bucket is None:
            continue  # 理论上不会发生（查询已经按时间过滤过），保险起见

        bucket["questions"] += 1
        bucket["tokens"] += (prompt_tokens or 0) + (completion_tokens or 0)
        if cache_hit:
            bucket["cache_hits"] += 1
        else:
            bucket["_latency_sum"] += latency_ms or 0
            bucket["_latency_count"] += 1

    result = []
    for day in sorted(buckets):
        bucket = buckets[day]
        count = bucket.pop("_latency_count")
        latency_sum = bucket.pop("_latency_sum")
        bucket["avg_latency_ms"] = int(latency_sum / count) if count else 0
        result.append(bucket)

    return result


# ============================================================
# 热门问题
# ============================================================
def get_top_questions(db: Session, limit: int = 10) -> list[dict]:
    """问得最多的问题排行。

    为什么要做归一化：用户问同一件事会带上不同的标点——
    「电池容量是多少」「电池容量是多少？」「电池容量是多少。」。
    不处理的话它们会被算成三个不同的问题，每个都只有一次，
    排行榜就失去意义了。用 rtrim 把句尾的标点去掉再分组。

    选 rtrim 而不是在 Python 里处理，是因为它 SQLite 和 PostgreSQL 都支持
    （PostgreSQL 也有 rtrim(字符串, 字符集) 这个双参数形式），
    能保住"代码可切换数据库"的约定。
    """
    limit = max(1, min(limit, 50))

    normalized = func.rtrim(QALog.question, "？?。.！!").label("question")

    rows = db.execute(
        select(
            normalized,
            func.count().label("times"),
            func.avg(QALog.latency_ms).label("avg_latency"),
        )
        .group_by(normalized)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()

    return [
        {
            "question": question,
            "times": int(times),
            "avg_latency_ms": int(avg_latency) if avg_latency is not None else 0,
        }
        for question, times, avg_latency in rows
    ]


# ============================================================
# 最近被点踩的回答
# ============================================================
def get_recent_feedback(db: Session, rating: int | None = None, limit: int = 20) -> list[dict]:
    """列出最近的反馈，给管理员排查用。

    光看"踩了 5 条"没用，得能看到**用户问了什么、系统答了什么**，
    才能判断是知识库缺资料、还是模型答偏了。这是这个列表存在的全部意义。

    这条查询要连 4 张表：feedback → messages → conversations → users，
    另外还要用子查询把"这条回答对应的问题是哪个"捞出来。

    为什么问题要用子查询找：消息表里提问和回答是两条独立记录，
    回答记录上并没有"我在回答哪个问题"的字段。只能取同一会话里
    id 比它小的最近一条用户消息——这是聊天记录天然的先后顺序。
    """
    limit = max(1, min(limit, 100))

    # 这条回答所回应的问题：同一会话里、id 比自己小的最后一条用户消息
    #
    # 必须用别名（aliased）把内层的 messages 和外层的分开。
    # 直接用同一个 Message 类写，SQLAlchemy 会以为内层要引用外层那张表，
    # 于是自动把它从 FROM 里"关联"掉，最后生成一条没有 FROM 的非法 SQL，
    # 报一个很难看懂的 auto-correlation 错误。
    question_message = aliased(Message)
    question_subquery = (
        select(question_message.content)
        .where(
            question_message.conversation_id == Message.conversation_id,
            question_message.role == MessageRole.USER,
            question_message.id < Feedback.message_id,
        )
        .order_by(question_message.id.desc())
        .limit(1)
        .correlate(Message, Feedback)
        .scalar_subquery()
    )

    stmt = (
        select(
            Feedback.id,
            Feedback.message_id,
            Feedback.rating,
            Feedback.comment,
            Feedback.created_at,
            Message.content,
            Conversation.title,
            User.username,
            question_subquery.label("question"),
        )
        .join(Message, Message.id == Feedback.message_id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        # 用 outerjoin：用户注销后 feedback 行还在（外键是 SET NULL），
        # 内连接会让这些记录凭空消失，统计数字对不上
        .outerjoin(User, User.id == Feedback.user_id)
        .order_by(Feedback.created_at.desc())
        .limit(limit)
    )

    if rating is not None:
        stmt = stmt.where(Feedback.rating == rating)

    rows = db.execute(stmt).all()

    return [
        {
            "id": row.id,
            "message_id": row.message_id,
            "rating": row.rating,
            "comment": row.comment,
            # 回答正文可能上千字，列表里只展示开头一段。
            # 完整内容对判断问题类型没帮助，还占满屏幕
            "answer_excerpt": (row.content or "")[:120],
            "conversation_title": row.title or "",
            "question": row.question or "",
            "username": row.username,
            "created_at": row.created_at,
        }
        for row in rows
    ]


# ============================================================
# 个人统计（普通用户也能看自己的）
# ============================================================
def get_my_stats(db: Session, user_id: int) -> dict:
    """当前用户的个人问答统计，个人中心页面用。"""
    qa_total = (
        db.scalar(
            select(func.count()).select_from(QALog).where(QALog.user_id == user_id)
        )
        or 0
    )
    qa_cache_hits = (
        db.scalar(
            select(func.count())
            .select_from(QALog)
            .where(QALog.user_id == user_id, QALog.cache_hit.is_(True))
        )
        or 0
    )
    conversation_count = (
        db.scalar(
            select(func.count())
            .select_from(Conversation)
            .where(Conversation.user_id == user_id)
        )
        or 0
    )
    message_count = (
        db.scalar(
            select(func.count())
            .select_from(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(Conversation.user_id == user_id)
        )
        or 0
    )
    prompt_tokens = (
        db.scalar(
            select(func.coalesce(func.sum(QALog.prompt_tokens), 0)).where(
                QALog.user_id == user_id
            )
        )
        or 0
    )
    completion_tokens = (
        db.scalar(
            select(func.coalesce(func.sum(QALog.completion_tokens), 0)).where(
                QALog.user_id == user_id
            )
        )
        or 0
    )
    avg_latency = db.scalar(
        select(func.avg(QALog.latency_ms)).where(
            QALog.user_id == user_id, QALog.cache_hit.is_(False)
        )
    )

    likes = (
        db.scalar(
            select(func.count())
            .select_from(Feedback)
            .where(Feedback.user_id == user_id, Feedback.rating == 1)
        )
        or 0
    )
    dislikes = (
        db.scalar(
            select(func.count())
            .select_from(Feedback)
            .where(Feedback.user_id == user_id, Feedback.rating == -1)
        )
        or 0
    )

    return {
        "qa_total": qa_total,
        "cache_hits": qa_cache_hits,
        "cache_hit_rate": _percent(qa_cache_hits, qa_total),
        "conversation_count": conversation_count,
        "message_count": message_count,
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(prompt_tokens) + int(completion_tokens),
        "avg_latency_ms": int(avg_latency) if avg_latency is not None else 0,
        "avg_first_token_ms": _get_my_avg_first_token(db, user_id),
        "likes": likes,
        "dislikes": dislikes,
    }


# ============================================================
# 运行时状态
# ============================================================
def get_runtime_stats() -> dict:
    """服务当前的运行时状态（缓存占用、并发数）。

    **这些是进程内的数字，重启服务就归零**，和数据库里的累计值完全是两回事。
    放在看板上是为了证明缓存和并发控制确实在工作，而不是写在文档里的摆设。

    注意这里读的是"缓存里现在有多少条、淘汰过多少条"，
    而"缓存命中率"用的是数据库里的 qa_logs.cache_hit。
    两者不重复：前者看容量水位，后者看实际效果。
    """
    from app.core import cache as answer_cache
    from app.core import rate_limit

    cache_stats = answer_cache.get_cache_stats()
    concurrency_stats = rate_limit.chat_concurrency.stats()

    return {
        "cache_entries": cache_stats.get("size", 0),
        "cache_maxsize": cache_stats.get("maxsize", 0),
        "cache_evictions": cache_stats.get("evictions", 0),
        "active_concurrency": concurrency_stats.get("total_in_flight", 0),
    }


def _get_my_avg_first_token(db: Session, user_id: int) -> int:
    """当前用户的平均首字延迟。逻辑同 get_avg_first_token_ms，多了用户过滤。"""
    rows = db.scalars(
        select(QALog.stage_latency)
        .where(QALog.user_id == user_id, QALog.stage_latency.is_not(None))
        .order_by(QALog.id.desc())
        .limit(FIRST_TOKEN_SAMPLE_SIZE)
    ).all()
    values: list[int] = []
    for raw in rows:
        try:
            value = json.loads(raw).get("first_token")
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue
        if isinstance(value, int) and value >= 0:
            values.append(value)
    return int(sum(values) / len(values)) if values else 0
