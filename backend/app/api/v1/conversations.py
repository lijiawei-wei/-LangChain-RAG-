"""会话管理接口。

所有接口都只操作「当前登录用户自己的」会话——归属校验在
conversation_service.get_conversation 里统一做，这一层不用重复写。
"""

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUser, DbSession
from app.rag.citations import from_snapshot
from app.schemas.chat import ConversationCreate, ConversationInfo, ConversationUpdate, MessageInfo
from app.schemas.common import ApiResponse, PageResult
from app.services import conversation_service, feedback_service

router = APIRouter(prefix="/conversations", tags=["会话"])


def _to_message_info(message, my_feedback: int | None = None) -> MessageInfo:
    """把消息记录转成接口返回结构。

    数据库里 citations 存的是 JSON 字符串，这里解析成对象列表再返回，
    前端拿到就能直接渲染，不用自己解析一遍。

    my_feedback 是"我对这条消息的评价"，由调用方批量查好后传进来——
    放在这里查会变成每条消息一次数据库往返（N+1 查询）。
    """
    return MessageInfo(
        id=message.id,
        role=message.role,
        content=message.content,
        citations=from_snapshot(message.citations),
        prompt_tokens=message.prompt_tokens,
        completion_tokens=message.completion_tokens,
        latency_ms=message.latency_ms,
        created_at=message.created_at,
        my_feedback=my_feedback,
    )


@router.get("", response_model=ApiResponse[PageResult[ConversationInfo]], summary="我的会话列表")
def list_conversations(
    db: DbSession,
    user: CurrentUser,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keyword: str | None = Query(None, description="按标题搜索"),
) -> ApiResponse[PageResult[ConversationInfo]]:
    """分页获取当前用户的会话列表。只能看到自己的。"""
    items, total = conversation_service.list_conversations(db, user.id, page, page_size, keyword)
    return ApiResponse.ok(
        PageResult[ConversationInfo](
            items=[ConversationInfo.model_validate(c) for c in items],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.post(
    "",
    response_model=ApiResponse[ConversationInfo],
    status_code=status.HTTP_201_CREATED,
    summary="新建会话",
)
def create_conversation(
    payload: ConversationCreate,
    db: DbSession,
    user: CurrentUser,
) -> ApiResponse[ConversationInfo]:
    """新建一个会话。

    kb_id 传了表示这个会话固定检索某个知识库；不传表示检索全部知识库。
    """
    conversation = conversation_service.create_conversation(
        db, user.id, kb_id=payload.kb_id, title=payload.title
    )
    return ApiResponse.ok(ConversationInfo.model_validate(conversation), message="会话已创建")


@router.get(
    "/{conversation_id}/messages",
    response_model=ApiResponse[list[MessageInfo]],
    summary="会话的历史消息",
)
def list_messages(
    conversation_id: int,
    db: DbSession,
    user: CurrentUser,
    limit: int = Query(50, ge=1, le=200, description="最多取多少条"),
    before_id: int | None = Query(
        None, description="取这个 ID 之前的消息（往上翻历史时用）"
    ),
) -> ApiResponse[list[MessageInfo]]:
    """拉取会话的历史消息，按时间正序返回。

    用 before_id 做游标分页而不是页码分页：聊天记录在增长，
    页码分页在往上翻的时候会因为新消息插入而错位、看到重复内容。
    """
    messages, _total = conversation_service.list_messages(
        db, conversation_id, user.id, limit, before_id
    )

    # 一次性把这批消息里"我评价过的"查出来，避免逐条查库
    feedback_map = feedback_service.get_my_feedback_map(
        db, user.id, [m.id for m in messages]
    )

    return ApiResponse.ok(
        [_to_message_info(m, feedback_map.get(m.id)) for m in messages]
    )


@router.patch(
    "/{conversation_id}",
    response_model=ApiResponse[ConversationInfo],
    summary="重命名会话",
)
def update_conversation(
    conversation_id: int,
    payload: ConversationUpdate,
    db: DbSession,
    user: CurrentUser,
) -> ApiResponse[ConversationInfo]:
    conversation = conversation_service.update_conversation(
        db, conversation_id, user.id, title=payload.title, kb_id=payload.kb_id
    )
    return ApiResponse.ok(ConversationInfo.model_validate(conversation), message="已更新")


@router.delete("/{conversation_id}", response_model=ApiResponse[None], summary="删除会话")
def delete_conversation(
    conversation_id: int, db: DbSession, user: CurrentUser
) -> ApiResponse[None]:
    """删除会话，它下面的所有消息会一并删除。"""
    conversation_service.delete_conversation(db, conversation_id, user.id)
    return ApiResponse.ok(message="会话已删除")


@router.delete("", response_model=ApiResponse[dict], summary="清空我的全部会话")
def clear_conversations(db: DbSession, user: CurrentUser) -> ApiResponse[dict]:
    """清空当前用户的所有会话。不可恢复。"""
    count = conversation_service.clear_conversations(db, user.id)
    return ApiResponse.ok({"deleted": count}, message=f"已清空 {count} 个会话")
