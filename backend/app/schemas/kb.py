"""知识库相关的请求/响应数据结构。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KnowledgeBaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(None, max_length=500, description="描述")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("知识库名称不能为空")
        return v


class KnowledgeBaseUpdate(BaseModel):
    """更新知识库。字段都是可选的，传了哪个就改哪个。"""

    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=500)

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("知识库名称不能为空")
        return v


class KnowledgeBaseInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    doc_count: int
    chunk_count: int
    created_at: datetime
    updated_at: datetime
