"""用户表。"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.utils.time import utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    username: Mapped[str] = mapped_column(
        String(50), unique=True, index=True, nullable=False
    )

    # 只存 bcrypt 哈希，永远不存明文密码
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    # 角色：user 普通用户（只能问答） / admin 管理员（可管理知识库）
    role: Mapped[str] = mapped_column(String(20), default="user", nullable=False)

    # 账号是否启用。预留的封禁能力，管理员可在数据库层禁用某个账号
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 令牌版本号。用户每次修改密码就 +1，于是此前签发的所有 JWT 都失效，
    # 强迫其它设备重新登录。这是「改密后旧令牌立即作废」的实现方式。
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # 删用户时级联删除其所有会话和消息，避免留下孤儿数据
    conversations: Mapped[list["Conversation"]] = relationship(  # noqa: F821
        back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role}>"
