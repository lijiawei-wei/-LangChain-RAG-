"""答案反馈（点赞/点踩）的业务逻辑。

这个模块存在的意义不只是"收集好评差评"，更实际的用途是**发现知识库的盲区**：
管理员看到某类问题集中被点踩，就知道该补哪方面的资料了。
"""

import logging

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.feedback import Feedback
from app.models.message import Message, MessageRole
from app.schemas.feedback import FeedbackCreate

logger = logging.getLogger(__name__)


def _get_rateable_message(db: Session, message_id: int, user_id: int) -> Message:
    """取出要评价的消息，并确认「这条消息归当前用户管」。

    两道校验，缺一不可：
      1. **归属**：消息所在的会话必须是这个用户的。少了这道，
         任何登录用户都能给别人的回答点赞点踩（甚至刷差评）。
      2. **角色**：只能评价助手的回答，不能评价自己的提问。
         评价用户自己的提问没有任何意义，还会污染统计。

    越权时返回 404 而不是 403，理由和会话模块一致：
    403 等于承认"这条消息存在，只是不归你"，可以被用来探测数据规模。
    """
    message = db.get(Message, message_id)

    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")

    # 顺着 消息 → 会话 找到归属人
    conversation = db.get(Conversation, message.conversation_id)
    if conversation is None or conversation.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="消息不存在")

    if message.role != MessageRole.ASSISTANT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="只能对助手的回答进行评价",
        )

    return message


def submit_feedback(
    db: Session, message_id: int, user_id: int, payload: FeedbackCreate
) -> Feedback:
    """提交或修改一条反馈。

    **是"覆盖"而不是"新增"**：数据库上有 (message_id, user_id) 的唯一约束，
    同一个用户对同一条回答只保留最后一次的态度。

    为什么要覆盖而不是每次新增一行：
      - 用户点了赞又想改成踩，如果新增一行，统计时会同时算作一赞一踩
      - 反复点击会让点赞数膨胀，看板上的满意度就失去意义了

    所以用户改主意时，第二次点击是"取消"（前端会调 DELETE），
    第三次点击才是重新表态——不管哪种，库里始终只有一行。
    """
    _get_rateable_message(db, message_id, user_id)

    existing = db.scalar(
        select(Feedback).where(
            Feedback.message_id == message_id, Feedback.user_id == user_id
        )
    )

    if existing is not None:
        existing.rating = payload.rating
        existing.comment = payload.comment
        db.commit()
        db.refresh(existing)
        return existing

    feedback = Feedback(
        message_id=message_id,
        user_id=user_id,
        rating=payload.rating,
        comment=payload.comment,
    )
    db.add(feedback)

    try:
        db.commit()
    except IntegrityError:
        # 用户快速连点了两下，两个请求都查不到已有记录、都走了插入分支，
        # 第二个会撞上唯一约束。这不是错误，是并发下的正常竞争，
        # 退回去改成更新即可（此刻记录一定已经存在了）。
        db.rollback()
        existing = db.scalar(
            select(Feedback).where(
                Feedback.message_id == message_id, Feedback.user_id == user_id
            )
        )
        if existing is None:
            # 理论上不会走到这里。真走到了说明约束和查询对不上，
            # 这种时候宁可报错，也不要假装成功——前端会显示"评价失败"，
            # 比默默吞掉、用户以为记录下来了要好
            raise
        existing.rating = payload.rating
        existing.comment = payload.comment
        db.commit()
        db.refresh(existing)
        return existing

    db.refresh(feedback)
    logger.info("用户 %s 对消息 %s 评价 %s", user_id, message_id, payload.rating)
    return feedback


def cancel_feedback(db: Session, message_id: int, user_id: int) -> None:
    """取消自己对某条回答的评价。

    用户点已选中的那个按钮（比如已经点过赞，再点一次赞）时触发。
    """
    _get_rateable_message(db, message_id, user_id)

    feedback = db.scalar(
        select(Feedback).where(
            Feedback.message_id == message_id, Feedback.user_id == user_id
        )
    )
    if feedback is None:
        # 本来就没评价过，当作已经取消处理。这里**不报 404**：
        # 用户想要的结果就是"没有我的评价"，这个状态已经达成了。
        # 报错只会让前端困惑——尤其是网络重试导致的重复请求
        return

    db.delete(feedback)
    db.commit()


def get_my_feedback_map(
    db: Session, user_id: int, message_ids: list[int]
) -> dict[int, int]:
    """批量查询「我对这批消息的评价」，返回 {消息ID: 1 或 -1}。

    为什么要批量查而不是逐条查：
    聊天记录一屏有几十条消息，逐条查就是几十次数据库往返（N+1 查询）。
    虽然本项目数据量小、感觉不出来，但这是很容易避免的坏习惯。

    前端靠这个返回值把"我点过赞的那条"高亮出来，
    否则刷新页面后按钮会全部变回未选中状态，用户以为自己没评价过。
    """
    if not message_ids:
        return {}

    rows = db.execute(
        select(Feedback.message_id, Feedback.rating).where(
            Feedback.user_id == user_id, Feedback.message_id.in_(message_ids)
        )
    ).all()

    return {message_id: rating for message_id, rating in rows}
