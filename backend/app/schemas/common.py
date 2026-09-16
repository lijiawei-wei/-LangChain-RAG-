"""通用数据结构：统一响应包装和分页。"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """统一响应格式。

    所有接口返回同样的外壳 {code, message, data}，前端拦截器就能统一处理
    错误提示，不用每个接口都写一遍判断逻辑。
    """

    code: int = 0
    message: str = "ok"
    data: T | None = None

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok") -> "ApiResponse[T]":
        return cls(code=0, message=message, data=data)

    @classmethod
    def fail(cls, message: str, code: int = -1) -> "ApiResponse[T]":
        return cls(code=code, message=message, data=None)


class PageParams(BaseModel):
    """分页查询参数的公共部分。

    对 page 和 page_size 做上限约束，防止前端传 page_size=1000000 把库拖垮。
    """

    page: int = Field(1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(20, ge=1, le=100, description="每页条数，最大 100")

    @property
    def offset(self) -> int:
        """转成 SQL 的 OFFSET。"""
        return (self.page - 1) * self.page_size


class PageResult(BaseModel, Generic[T]):
    """分页返回结构。"""

    items: list[T]
    total: int
    page: int
    page_size: int

    @property
    def pages(self) -> int:
        """总页数，向上取整。"""
        return (self.total + self.page_size - 1) // self.page_size if self.page_size else 0
