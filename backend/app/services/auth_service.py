"""认证业务逻辑：注册、登录、改密、签发令牌。

把业务逻辑和 HTTP 层分开写，好处是同一套逻辑既能被 REST 接口调用，
将来也能被命令行脚本或定时任务复用，测试时也不必启动整个 Web 服务。
"""

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import create_token, hash_password, verify_password
from app.models.user import User
from app.schemas.auth import TokenResponse, UserInfo
from app.utils.time import utcnow


def get_user_by_username(db: Session, username: str) -> User | None:
    """按用户名查用户。"""
    return db.scalar(select(User).where(User.username == username))


def register_user(db: Session, username: str, password: str) -> User:
    """注册新用户。

    注册出来的账号一律是普通用户（role=user），管理员只能由种子数据产生。
    这是有意的安全设计：如果注册接口能指定角色，任何人都能把自己注册成管理员。
    """
    if get_user_by_username(db, username) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该用户名已被注册，请换一个"
        )

    user = User(
        username=username,
        password_hash=hash_password(password),
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, username: str, password: str) -> User:
    """校验用户名密码，成功返回用户对象。

    两种失败情况（用户不存在 / 密码错误）返回完全相同的提示，
    是为了防止攻击者用「用户名是否存在」这个差异来枚举系统里的账号。
    """
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
    )

    user = get_user_by_username(db, username)
    if user is None:
        # 即使用户不存在也走一次哈希校验，让响应耗时与密码错误时接近，
        # 避免通过响应时间快慢推断出用户名是否存在（时序攻击）。
        verify_password(password, "$2b$12$" + "x" * 53)
        raise invalid

    if not verify_password(password, user.password_hash):
        raise invalid

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="账号已被停用，请联系管理员"
        )

    # 记录登录时间，管理端可据此判断用户活跃度
    user.last_login_at = utcnow()
    db.commit()
    db.refresh(user)
    return user


def change_password(db: Session, user: User, old_password: str, new_password: str) -> None:
    """修改密码。

    改完会做两件事：
      1. token_version + 1 —— 让此前签发的所有令牌立刻失效（含其它设备）
      2. 返回的新令牌由接口层下发，用户当前设备无缝继续使用
    """
    if not verify_password(old_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="当前密码不正确"
        )

    if verify_password(new_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="新密码不能与当前密码相同"
        )

    user.password_hash = hash_password(new_password)
    user.token_version += 1  # 关键：旧令牌全部作废
    db.commit()
    db.refresh(user)


def build_token_response(user: User) -> TokenResponse:
    """为用户签发一对令牌，并组装成响应体。

    令牌里带上 role，前端拿到后可以直接判断要不要显示「知识库管理」入口，
    不用额外再请求一次用户信息。但注意：前端显示控制只是体验优化，
    真正的权限校验始终在服务端（require_admin），前端隐藏不等于安全。
    """
    access_token = create_token(
        subject=user.id,
        token_type="access",
        token_version=user.token_version,
        extra_claims={"role": user.role, "username": user.username},
    )
    refresh_token = create_token(
        subject=user.id,
        token_type="refresh",
        token_version=user.token_version,
    )

    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserInfo.model_validate(user),
    )
