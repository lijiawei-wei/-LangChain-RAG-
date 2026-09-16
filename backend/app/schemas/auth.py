"""认证相关的请求/响应数据结构。

用 Pydantic 模型而不是裸 dict，好处是 FastAPI 会自动做参数校验，
前端传了不合法的数据会直接返回 422 和明确的错误信息，不用手写 if 判断。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=50, description="用户名")
    password: str = Field(..., min_length=6, max_length=128, description="密码")

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """用户名只允许字母、数字、下划线、连字符。

        为什么要限制：用户名会出现在日志、URL 等地方，放开特殊字符容易
        带来注入和显示问题。同时也避免用户注册出全空格的用户名。
        """
        v = v.strip()
        if not v:
            raise ValueError("用户名不能为空")
        if not all(c.isalnum() or c in "_-" for c in v):
            raise ValueError("用户名只能包含字母、数字、下划线或连字符")
        return v


class LoginRequest(BaseModel):
    username: str = Field(..., description="用户名")
    password: str = Field(..., description="密码")


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., description="刷新令牌")


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., description="当前密码")
    new_password: str = Field(..., min_length=6, max_length=128, description="新密码")

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: str) -> str:
        """新密码不能和旧密码一样，否则改了等于没改。"""
        if not v.strip():
            raise ValueError("新密码不能为空")
        return v


class UserInfo(BaseModel):
    """返回给前端的用户信息。刻意不包含 password_hash。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: str
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None = None


class TokenResponse(BaseModel):
    """登录/刷新的返回体。"""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="访问令牌有效期（秒）")
    user: UserInfo
