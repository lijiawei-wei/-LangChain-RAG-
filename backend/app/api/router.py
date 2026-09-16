"""API 路由汇总。

所有 v1 版本的路由都在这里注册到同一个 router 上，main.py 只需要挂一次。
新增模块时在这里加一行 include_router 即可。
"""

from fastapi import APIRouter

from app.api.v1 import (
    auth,
    chat,
    conversations,
    documents,
    feedback,
    knowledge_bases,
    stats,
)

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(knowledge_bases.router)
api_router.include_router(documents.router)
api_router.include_router(conversations.router)
api_router.include_router(chat.router)

# 答案反馈（点赞/点踩）：登录用户都能用，但只能评价自己的回答
api_router.include_router(feedback.router)

# 统计：/stats/me 所有登录用户可用；/admin/stats/* 仅管理员
api_router.include_router(stats.router)
api_router.include_router(stats.admin_router)
