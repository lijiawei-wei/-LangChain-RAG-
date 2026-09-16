"""数据库连接与会话管理。

用 SQLAlchemy 2.0 风格。写在 DAO 层的代码不依赖 SQLite 特有语法，
将来要换成 PostgreSQL，只需要改 config.py 里的 DATABASE_URL 一行。
"""

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings

# SQLite 特有的连接参数：FastAPI 是多线程处理请求的，
# 默认 SQLite 连接不允许跨线程复用，所以要关掉这个检查。
# 换成 PostgreSQL 时这个参数会被忽略，不影响。
_connect_args = (
    {"check_same_thread": False} if settings.DATABASE_URL.startswith("sqlite") else {}
)

engine = create_engine(
    settings.DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,  # 取连接前先探活，避免用到已被服务端断开的连接
    # 连接池大小。SQLite 是文件锁模型，连接开太多反而会互相等锁，
    # 所以真正要控制的不是池子大小，而是后台任务的并发数
    # （见 config.py 里的 DOCUMENT_PROCESS_CONCURRENCY 说明）
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    # 池子满了之后的等待时间。设短一些让它快速失败：
    # 等 30 秒再报错，用户只会看到"文档卡住了"，根本联想不到是连接池问题
    pool_timeout=settings.DB_POOL_TIMEOUT,
    echo=False,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
    """每次新建 SQLite 连接时设置优化参数。

    这三条 PRAGMA 是 SQLite 做并发应用的关键调优：
      foreign_keys=ON  —— 打开外键约束（SQLite 默认是关的！不打开的话
                          删用户时关联数据不会级联清理，会留下脏数据）
      journal_mode=WAL —— 写前日志模式，读写不再互相阻塞，
                          多人同时用的时候明显更流畅
      synchronous=NORMAL —— 配合 WAL 使用，减少磁盘 fsync 次数，
                          速度更快，断电最多丢失最近一次事务（可接受）
    """
    if not settings.DATABASE_URL.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""


SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,  # 提交后对象仍可读，避免后续访问属性又去查一次库
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用的数据库会话。

    每个请求拿一个独立会话，请求结束（无论成功还是异常）都保证关闭，
    防止连接泄漏把连接池占满。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
