"""文档相关的请求/响应数据结构。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DocumentInfo(BaseModel):
    """文档列表里的一条记录。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    kb_id: int
    filename: str
    file_type: str
    file_size: int
    status: str
    chunk_count: int
    error_msg: str | None = None
    created_at: datetime
    indexed_at: datetime | None = None

    @property
    def size_display(self) -> str:
        """把字节数转成人类看得懂的大小。"""
        size = float(self.file_size)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"


class UploadResult(BaseModel):
    """上传单个文件的结果。

    上传是批量接口，每个文件单独返回结果，因为可能出现
    「这个成功了那个失败」的情况，用统一的结果结构前端才好逐个展示。
    """

    filename: str
    success: bool
    # 三种情况：created 新入库 / duplicated 重复文件跳过 / failed 处理失败
    result: str
    message: str
    document: DocumentInfo | None = None


class ChunkInfo(BaseModel):
    """分块预览里的一条。"""

    id: str
    content: str
    metadata: dict = Field(default_factory=dict)


class ChunkListResponse(BaseModel):
    """某个文档的分块预览结果。"""

    total: int
    items: list[ChunkInfo]
