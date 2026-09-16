"""ORM 模型统一出口。

为什么要把所有模型在这里 import 一遍：SQLAlchemy 只会在模型类「被导入执行」
时才把它注册到 Base.metadata。如果某个模型从没被 import 过，建表时就会被漏掉。
这里集中导入，保证 `Base.metadata.create_all()` 一定能建出全部表。
"""

from app.models.conversation import Conversation
from app.models.document import Document, DocumentStatus
from app.models.feedback import Feedback
from app.models.knowledge_base import KnowledgeBase
from app.models.message import Message, MessageRole
from app.models.qa_log import QALog
from app.models.user import User

__all__ = [
    "User",
    "KnowledgeBase",
    "Document",
    "DocumentStatus",
    "Conversation",
    "Message",
    "MessageRole",
    "Feedback",
    "QALog",
]
