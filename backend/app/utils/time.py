"""时间工具。

为什么单独封装：Python 3.12 起 `datetime.utcnow()` 被标记为废弃，
而 SQLite 又不保存时区信息。这里统一返回「不带时区标记的 UTC 时间」，
保证全项目时间口径一致，将来换 PostgreSQL 时也不会因为时区出问题。
"""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """返回当前 UTC 时间（naive，不带时区标记）。

    全项目所有入库时间都用这个函数，不要直接用 datetime.now()，
    否则服务器时区一变，历史数据的时间就全乱了。
    """
    return datetime.now(UTC).replace(tzinfo=None)
