"""知识库管理接口。

权限设计：
  - 查询类接口（列表、详情）对所有登录用户开放 —— 普通用户提问时需要
    先选择一个知识库，得能读到列表
  - 增删改接口仅管理员可用 —— 管理知识库内容是管理员专属职责
"""

from fastapi import APIRouter, Query, status

from app.core.deps import AdminUser, CurrentUser, DbSession
from app.schemas.common import ApiResponse, PageResult
from app.schemas.kb import KnowledgeBaseCreate, KnowledgeBaseInfo, KnowledgeBaseUpdate
from app.services import document_service, kb_service

router = APIRouter(prefix="/knowledge-bases", tags=["知识库"])


@router.get(
    "",
    response_model=ApiResponse[PageResult[KnowledgeBaseInfo]],
    summary="知识库列表（分页）",
)
def list_knowledge_bases(
    db: DbSession,
    _: CurrentUser,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(20, ge=1, le=100, description="每页条数"),
    keyword: str | None = Query(None, description="按名称模糊搜索"),
) -> ApiResponse[PageResult[KnowledgeBaseInfo]]:
    """分页获取知识库列表。所有登录用户都能调用。"""
    items, total = kb_service.list_knowledge_bases(db, page, page_size, keyword)
    return ApiResponse.ok(
        PageResult[KnowledgeBaseInfo](
            items=[KnowledgeBaseInfo.model_validate(kb) for kb in items],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get(
    "/all",
    response_model=ApiResponse[list[KnowledgeBaseInfo]],
    summary="获取全部知识库（不分页）",
)
def list_all(db: DbSession, _: CurrentUser) -> ApiResponse[list[KnowledgeBaseInfo]]:
    """不分页返回所有知识库，供问答页的知识库下拉框使用。

    单独做一个接口而不是复用分页接口，是因为下拉框需要一次性拿到全部选项，
    知识库数量本来就不多（通常十几个），没必要分页。
    """
    items = kb_service.list_all_for_select(db)
    return ApiResponse.ok([KnowledgeBaseInfo.model_validate(kb) for kb in items])


@router.post(
    "",
    response_model=ApiResponse[KnowledgeBaseInfo],
    status_code=status.HTTP_201_CREATED,
    summary="新建知识库（仅管理员）",
)
def create_knowledge_base(
    payload: KnowledgeBaseCreate,
    db: DbSession,
    user: AdminUser,
) -> ApiResponse[KnowledgeBaseInfo]:
    """新建一个知识库。"""
    kb = kb_service.create_knowledge_base(db, payload.name, payload.description, user.id)
    return ApiResponse.ok(KnowledgeBaseInfo.model_validate(kb), message="知识库创建成功")


@router.get(
    "/{kb_id}",
    response_model=ApiResponse[KnowledgeBaseInfo],
    summary="知识库详情",
)
def get_knowledge_base(
    kb_id: int, db: DbSession, _: CurrentUser
) -> ApiResponse[KnowledgeBaseInfo]:
    return ApiResponse.ok(KnowledgeBaseInfo.model_validate(kb_service.get_knowledge_base(db, kb_id)))


@router.get(
    "/{kb_id}/status",
    response_model=ApiResponse[dict],
    summary="知识库处理状态概览",
)
def get_status(kb_id: int, db: DbSession, _: AdminUser) -> ApiResponse[dict]:
    """统计知识库下各状态的文档数量。

    管理页靠它决定是否继续轮询：只要 in_progress 大于 0，
    就说明还有文档在处理，前端继续定时刷新；归零就停止轮询。
    """
    kb_service.get_knowledge_base(db, kb_id)
    return ApiResponse.ok(document_service.get_kb_status_summary(db, kb_id))


@router.patch(
    "/{kb_id}",
    response_model=ApiResponse[KnowledgeBaseInfo],
    summary="修改知识库（仅管理员）",
)
def update_knowledge_base(
    kb_id: int,
    payload: KnowledgeBaseUpdate,
    db: DbSession,
    _: AdminUser,
) -> ApiResponse[KnowledgeBaseInfo]:
    """重命名知识库或修改描述。"""
    kb = kb_service.update_knowledge_base(db, kb_id, payload.name, payload.description)
    return ApiResponse.ok(KnowledgeBaseInfo.model_validate(kb), message="修改成功")


@router.delete(
    "/{kb_id}",
    response_model=ApiResponse[None],
    summary="删除知识库（仅管理员）",
)
async def delete_knowledge_base(kb_id: int, db: DbSession, _: AdminUser) -> ApiResponse[None]:
    """删除知识库，连同它下面的所有文档和向量数据。

    这是不可恢复的操作，前端会弹二次确认框。
    """
    await kb_service.delete_knowledge_base(db, kb_id)
    return ApiResponse.ok(message="知识库已删除")
