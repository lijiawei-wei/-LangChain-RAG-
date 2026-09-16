"""答案反馈（点赞/点踩）的数据结构。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class FeedbackCreate(BaseModel):
    """提交一条反馈。"""

    # 用 Literal 而不是 int：不限制的话，客户端可以传 rating=5 或 rating=0，
    # 这些值会悄悄混进数据库，把看板上的满意度算成一个莫名其妙的比例。
    # 写成 Literal 后，非法值在入口就被 FastAPI 挡下并给出清晰提示。
    rating: Literal[1, -1] = Field(..., description="1 = 点赞，-1 = 点踩")
    comment: str | None = Field(None, max_length=500, description="补充说明，可不填")

    @field_validator("comment")
    @classmethod
    def clean_comment(cls, v: str | None) -> str | None:
        """去掉首尾空白；全空白视为没填。"""
        if v is None:
            return None
        v = v.strip()
        return v or None


class FeedbackInfo(BaseModel):
    """一条反馈记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    message_id: int
    rating: int
    comment: str | None = None
    created_at: datetime


class FeedbackDetail(BaseModel):
    """带上下文的一条反馈，供管理员排查用。

    光有 rating 说明不了问题——管理员需要看到"用户问了什么、系统答了什么、
    用户为什么不满"，才能判断是知识库缺资料还是模型答偏了。
    """

    id: int
    message_id: int
    rating: int
    comment: str | None = None

    # 被评价的那条回答（截断展示，完整内容对排查没帮助还占地方）
    answer_excerpt: str = ""
    # 回答所属会话的标题 + 那条会话里用户最近的一次提问
    conversation_title: str = ""
    question: str = ""
    # 提问人。用户注销后这条会变成 None（外键是 SET NULL）
    username: str | None = None

    created_at: datetime
