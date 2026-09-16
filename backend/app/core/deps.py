"""FastAPI 依赖注入：取当前用户、校验管理员权限。

这些函数通过 Depends() 挂到路由上，FastAPI 会自动调用并注入结果。
好处是权限校验只写一次，所有需要登录的接口都复用同一套逻辑。
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.security import decode_token
from app.db.base import get_db
from app.models.user import User

# auto_error=False：没带令牌时不自动报错，交给下面的函数给出统一的中文提示
_bearer_scheme = HTTPBearer(auto_error=False)

DbSession = Annotated[Session, Depends(get_db)]


def _unauthorized(detail: str) -> HTTPException:
    """构造 401 异常，并带上让浏览器清除本地令牌的标记。"""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)],
    db: DbSession,
) -> User:
    """解析请求头里的 JWT，返回对应用户。

    校验链条（任何一步不过都返回 401）：
      1. 请求头里有没有 Bearer 令牌
      2. 令牌签名对不对、有没有过期
      3. 是不是访问令牌（刷新令牌不能直接访问业务接口）
      4. 用户还在不在、有没有被停用
      5. 令牌版本号是否与用户当前版本一致 —— 这一步实现「改密后旧令牌作废」
    """
    if credentials is None or not credentials.credentials:
        raise _unauthorized("未提供登录凭证，请先登录")

    payload = decode_token(credentials.credentials)
    if payload is None:
        raise _unauthorized("登录凭证无效或已过期，请重新登录")

    if payload.get("type") != "access":
        raise _unauthorized("令牌类型不正确，请使用访问令牌")

    try:
        user_id = int(payload.get("sub", ""))
    except (TypeError, ValueError):
        raise _unauthorized("登录凭证格式异常，请重新登录") from None

    user = db.get(User, user_id)
    if user is None:
        raise _unauthorized("用户不存在，请重新登录")
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="账号已被停用，请联系管理员"
        )

    # 关键一步：比对令牌版本号。
    # 用户改密码后 token_version 会 +1，手里所有旧令牌的 ver 就对不上了，
    # 于是自动失效，强迫重新登录。
    if payload.get("ver") != user.token_version:
        raise _unauthorized("密码已变更，请重新登录")

    return user


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    """在「已登录」的基础上再要求管理员角色。

    知识库管理相关的接口全部挂这个依赖，普通用户访问会拿到 403。
    注意这里返回 403 而不是 401：401 的含义是「你还没登录」，
    403 才是「你登录了但没权限」，前端据此决定是跳登录页还是提示无权限。
    """
    if user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该操作仅限管理员，请联系管理员处理",
        )
    return user


# 简写别名，路由里直接写 CurrentUser / AdminUser 更简洁
CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin)]
