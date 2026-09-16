"""统计看板的数据结构。

关于总览接口为什么用 dict 而不是定义一个嵌套模型：它的结构是三层嵌套
（用户/文档/问答/反馈各一组），写出来要六七个类，但**没有任何字段需要校验**
——数据全是服务端自己算出来的，不存在"入参不合法"的情况。
这种纯输出的聚合结构，项目里已有先例（知识库状态概览就返回 dict）。
列表类的接口则定义了模型，因为它们要展示在接口文档里、前端照着渲染。
"""

from datetime import datetime

from pydantic import BaseModel, Field


class TrendItem(BaseModel):
    """趋势图上的一天。"""

    date: str = Field(..., description="本地日期，格式 YYYY-MM-DD")
    questions: int = Field(0, description="当天的问答次数")
    tokens: int = Field(0, description="当天的 token 消耗总量")
    cache_hits: int = Field(0, description="当天命中缓存、没走模型生成的次数")
    avg_latency_ms: int = Field(
        0, description="当天的平均响应延迟，**不含缓存命中**（命中的延迟恒为 0，混进来会失真）"
    )


class TopQuestion(BaseModel):
    """一条高频问题。"""

    question: str
    times: int = Field(0, description="被问到的次数")
    avg_latency_ms: int = 0


class MyStats(BaseModel):
    """个人问答统计。"""

    qa_total: int = 0
    cache_hits: int = 0
    cache_hit_rate: float = Field(0.0, description="缓存命中率，百分比")

    conversation_count: int = 0
    message_count: int = 0

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    avg_latency_ms: int = Field(0, description="平均响应延迟，不含缓存命中")
    avg_first_token_ms: int = Field(0, description="平均首字延迟：提问到看见第一个字")

    likes: int = 0
    dislikes: int = 0


class RuntimeStats(BaseModel):
    """服务当前的运行时状态。

    这些数字是**进程内的**，重启服务就归零——和数据库里的累计值不是一回事。
    放在看板上是为了演示缓存和限流确实在起作用，不是摆设。
    """

    cache_entries: int = Field(0, description="答案缓存里当前有多少条")
    cache_maxsize: int = Field(0, description="缓存容量上限")
    cache_evictions: int = Field(0, description="因为放不下被淘汰掉的条数")
    active_concurrency: int = Field(0, description="当前正在生成中的问答数")


class FeedbackDetailOut(BaseModel):
    """一条带上下文的反馈，供管理员排查。"""

    id: int
    message_id: int
    rating: int
    comment: str | None = None
    answer_excerpt: str = ""
    conversation_title: str = ""
    question: str = ""
    username: str | None = None
    created_at: datetime
