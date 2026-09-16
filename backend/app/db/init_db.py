"""数据库初始化：建表 + 创建管理员种子账号。

程序每次启动都会跑一遍，但它是「幂等」的——
已经建过的表不会重复建，已经存在的管理员不会重复创建，所以跑多少次都安全。
"""

import logging

from sqlalchemy import select

from app.core.config import settings
from app.core.security import hash_password
from app.db.base import Base, SessionLocal, engine
from app.models.user import User

# 导入所有模型，确保它们都被注册到 Base.metadata 上，否则建表会漏表
from app.models import *  # noqa: F401,F403

logger = logging.getLogger(__name__)


def create_tables() -> None:
    """按模型定义建出所有表（已存在的表会被跳过）。"""
    Base.metadata.create_all(bind=engine)


def seed_admin() -> None:
    """确保管理员账号存在。

    首次启动会创建 admin / 123456（可通过 .env 里的 ADMIN_USERNAME、
    ADMIN_PASSWORD 改）。之后每次启动只做检查，不会覆盖已改过的密码——
    这点很重要，否则管理员改了密码，一重启就被重置回默认值了。
    """
    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.username == settings.ADMIN_USERNAME))

        if admin is None:
            admin = User(
                username=settings.ADMIN_USERNAME,
                password_hash=hash_password(settings.ADMIN_PASSWORD),
                role="admin",
            )
            db.add(admin)
            db.commit()
            logger.info(
                "已创建管理员账号：%s（初始密码见 .env，请尽快修改）",
                settings.ADMIN_USERNAME,
            )
            return

        # 账号已存在，但角色不是管理员（比如被人用注册接口抢注了同名账号），
        # 这里纠正回管理员，保证系统始终有一个可用的管理入口。
        if admin.role != "admin":
            admin.role = "admin"
            db.commit()
            logger.warning("账号 %s 已存在但角色不是管理员，已自动修正", admin.username)


def init_db() -> None:
    """初始化数据库：建目录 → 建表 → 确保管理员存在 → 清理僵尸文档。"""
    settings.ensure_dirs()
    create_tables()
    seed_admin()

    # 放在最后：这一步要查 documents 表，得先保证表已经建好
    from app.services.document_service import reset_stuck_documents

    reset_stuck_documents()

    logger.info("数据库初始化完成：%s", settings.DATABASE_URL)
