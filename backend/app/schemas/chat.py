"""会话与问答相关的数据结构。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ============================================================
# 会话
# ============================================================
class ConversationCreate(BaseModel):
    title: str | None = Field(None, max_length=200, description="会话标题，不填则自动生成")
    kb_id: int | None = Field(None, description="绑定哪个知识库；不填表示检索全部")


class ConversationUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    kb_id: int | None = None

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("会话标题不能为空")
        return v


class ConversationInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    kb_id: int | None = None
    message_count: int
    created_at: datetime
    updated_at: datetime


# ============================================================
# 消息
# ============================================================
class MessageInfo(BaseModel):
    """一条对话消息。

    citations 直接返回解析好的对象列表（而不是原始 JSON 字符串），
    这样前端拿到就能渲染，不用自己再解析一遍。
    """

    id: int
    role: str
    content: str
    citations: list[dict] = Field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    created_at: datetime

    # 当前用户对这条消息的评价：1 = 点过赞，-1 = 点过踩，None = 没评价过。
    # 必须随消息一起返回，否则用户刷新页面后按钮会全部变回未选中，
    # 他会以为自己之前的评价丢了（其实还在库里）。
    my_feedback: int | None = None


# ============================================================
# 问答
# ============================================================
class ChatRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000, description="用户提问")

    # 两种用法：
    #   传 conversation_id —— 在已有会话里继续追问，会自动带上历史
    #   传 kb_id           —— 开一个新会话
    # 都不传时会在「全部知识库」里开新会话
    conversation_id: int | None = None
    kb_id: int | None = None

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("问题不能为空")
        return v


class Citation(BaseModel):
    """一条引用来源。"""

    index: int = Field(..., description="编号，对应回答里的 [1] 这样的标注")
    chunk_id: str = ""
    doc_id: int | None = None
    filename: str = ""
    page: int | None = None
    sheet: str | None = None
    source_label: str = Field("", description="给人看的来源描述，如「手册.pdf 第 3 页」")
    score: float = 0.0
    text: str = Field("", description="原文片段")
    truncated: bool = False
    used: bool = Field(False, description="这条回答是否真的引用了它")


class ChatResponse(BaseModel):
    """一次问答的完整结果。"""

    conversation_id: int
    user_message_id: int
    assistant_message_id: int
    answer: str
    citations: list[Citation] = Field(default_factory=list)

    # 性能指标，前端可以显示"耗时 1.2 秒"
    latency_ms: int = 0
    stage_latency: dict = Field(default_factory=dict)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    # 实际用于检索的问题。开启查询改写后，它可能和用户原提问不同，
    # 展示出来能让用户明白系统"理解"成了什么
    retrieved_query: str = ""
