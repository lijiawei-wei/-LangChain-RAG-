"""文档管理接口。全部仅管理员可用。

文档是整个知识库的数据来源，能改文档就等于能改系统回答用户的内容，
所以这一组接口全部挂 AdminUser 依赖，普通用户访问一律 403。
"""

import logging

from fastapi import APIRouter, BackgroundTasks, File, Form, Query, UploadFile, status

from app.core.config import settings
from app.core.deps import AdminUser, DbSession
from app.schemas.common import ApiResponse, PageResult
from app.schemas.document import (
    ChunkInfo,
    ChunkListResponse,
    DocumentInfo,
    UploadResult,
)
from app.services import document_service, kb_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/documents", tags=["文档"])


def _friendly_upload_error(error: Exception) -> str:
    """把上传过程中冒出来的异常转成能给用户看的一句话。

    **为什么要做这个转换**：以前这里直接把 `str(error)` 返回给前端。
    对 SQLAlchemy 的异常来说，`str(e)` 里带着**完整的 SQL 语句、
    表名、字段名和参数值**，等于把数据库结构暴露给了浏览器。
    这既和 main.py 里"不向前端泄露内部细节"的约定冲突，
    也会让用户看到一大段完全看不懂的东西——
    而现在前端会把这条消息**显著地展示出来**，所以更不能含糊。

    真正的原因记在服务端日志里（调用处已经 logger.warning 了），
    返回给用户的只保留"大概是什么类别的错"。
    """
    # HTTPException 是我们自己主动抛的，detail 本来就是给人看的中文
    detail = getattr(error, "detail", None)
    if isinstance(detail, str) and detail:
        return detail

    name = type(error).__name__
    if "IntegrityError" in name:
        # 并发上传同一个文件时，两个请求都通过了查重、第二个撞上唯一约束
        return "这个文件正在被另一个请求处理，请稍后刷新列表确认是否已上传"
    if "OperationalError" in name:
        return "数据库繁忙，请稍后重试"
    if "UnsupportedFileType" in name:
        return "文件类型不支持"
    return "上传失败，具体原因请查看服务端日志"


@router.post(
    "/upload",
    response_model=ApiResponse[list[UploadResult]],
    status_code=status.HTTP_201_CREATED,
    summary="上传文档（仅管理员）",
)
async def upload_documents(
    background_tasks: BackgroundTasks,
    db: DbSession,
    user: AdminUser,
    kb_id: int = Form(..., description="归属的知识库 ID"),
    files: list[UploadFile] = File(..., description="要上传的文件，可多选"),
) -> ApiResponse[list[UploadResult]]:
    """上传一个或多个文档。

    关键设计：**接口立刻返回，实际处理放后台**。

    上传后文档进入「待处理」状态，解析和向量化在后台跑，
    前端通过轮询状态接口显示进度（待处理 → 解析中 → 分块中 → 向量化中 → 完成）。

    为什么要这样：一份几十页的 PDF，解析加向量化要几十秒。
    如果接口一直等着处理完再返回，用户体验上会以为页面卡死了，
    而且连接被长时间占着，并发能力会明显下降。
    """
    kb_service.get_knowledge_base(db, kb_id)  # 先确认知识库存在

    results: list[UploadResult] = []
    to_process: list[int] = []

    for upload in files:
        filename = upload.filename or "未命名文件"

        try:
            content = await upload.read()
        except Exception as e:
            logger.warning("读取上传文件 %s 失败：%s", filename, e)
            results.append(
                UploadResult(
                    filename=filename,
                    success=False,
                    result="failed",
                    message=f"读取文件失败：{e}",
                )
            )
            continue

        # 单个文件大小限制。放在读完之后检查是因为要拿到实际大小，
        # 而且 FastAPI 没有内置的请求体大小限制
        size_mb = len(content) / 1024 / 1024
        if size_mb > settings.MAX_UPLOAD_SIZE_MB:
            results.append(
                UploadResult(
                    filename=filename,
                    success=False,
                    result="failed",
                    message=f"文件 {size_mb:.1f}MB，超过 {settings.MAX_UPLOAD_SIZE_MB}MB 上限",
                )
            )
            continue

        try:
            doc, outcome = document_service.upload_document(
                db, kb_id, filename, content, user.id
            )

            if outcome == "duplicated":
                results.append(
                    UploadResult(
                        filename=filename,
                        success=True,
                        result="duplicated",
                        message=f"内容与已有文档「{doc.filename}」完全相同，已跳过（不会重复占用空间）",
                        document=DocumentInfo.model_validate(doc),
                    )
                )
            else:
                to_process.append(doc.id)
                results.append(
                    UploadResult(
                        filename=filename,
                        success=True,
                        result="created",
                        message="上传成功，正在后台处理",
                        document=DocumentInfo.model_validate(doc),
                    )
                )
        except Exception as e:
            # 单个文件失败不该让整批上传都失败，所以在这里兜住
            logger.warning("上传文件 %s 失败：%s", filename, e)
            db.rollback()
            results.append(
                UploadResult(
                    filename=filename,
                    success=False,
                    result="failed",
                    message=_friendly_upload_error(e),
                )
            )

    # 把新文档的处理任务挂到后台队列。
    # 注意是等所有文件都存好之后才统一挂任务，这样如果中途出错，
    # 已经成功入库的文件照样会被处理，不会因为一次异常全部落空。
    for doc_id in to_process:
        background_tasks.add_task(document_service.process_document, doc_id)

    created = sum(1 for r in results if r.result == "created")
    duplicated = sum(1 for r in results if r.result == "duplicated")
    message = f"共 {len(results)} 个文件：{created} 个已入库，{duplicated} 个重复跳过"
    return ApiResponse.ok(results, message=message)


@router.get(
    "",
    response_model=ApiResponse[PageResult[DocumentInfo]],
    summary="文档列表（仅管理员）",
)
def list_documents(
    db: DbSession,
    _: AdminUser,
    kb_id: int | None = Query(None, description="按知识库筛选"),
    status_filter: str | None = Query(
        None, alias="status", description="按状态筛选：pending/parsing/chunking/embedding/ready/failed"
    ),
    keyword: str | None = Query(None, description="按文件名模糊搜索"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> ApiResponse[PageResult[DocumentInfo]]:
    """分页查询文档列表。"""
    items, total = document_service.list_documents(
        db, kb_id, page, page_size, status_filter, keyword
    )
    return ApiResponse.ok(
        PageResult[DocumentInfo](
            items=[DocumentInfo.model_validate(doc) for doc in items],
            total=total,
            page=page,
            page_size=page_size,
        )
    )


@router.get(
    "/{doc_id}/chunks",
    response_model=ApiResponse[ChunkListResponse],
    summary="查看文档的分块（仅管理员）",
)
def get_document_chunks(
    doc_id: int,
    db: DbSession,
    _: AdminUser,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> ApiResponse[ChunkListResponse]:
    """预览某份文档被切成了哪些块。

    用途：管理员可以直观看到分块效果好不好。如果切得太碎或太整，
    就说明分块参数需要调整，这是调优 RAG 效果最直接的手段。
    """
    items, total = document_service.get_document_chunks(db, doc_id, limit, offset)
    return ApiResponse.ok(
        ChunkListResponse(
            total=total,
            items=[ChunkInfo(**item) for item in items],
        )
    )


@router.delete(
    "/{doc_id}",
    response_model=ApiResponse[None],
    summary="删除文档（仅管理员）",
)
async def delete_document(doc_id: int, db: DbSession, _: AdminUser) -> ApiResponse[None]:
    """删除文档，同时清除它的向量数据和磁盘原文件。"""
    await document_service.delete_document(db, doc_id)
    return ApiResponse.ok(message="文档已删除")


@router.post(
    "/{doc_id}/reindex",
    response_model=ApiResponse[DocumentInfo],
    summary="重建索引（仅管理员）",
)
async def reindex_document(
    doc_id: int,
    background_tasks: BackgroundTasks,
    db: DbSession,
    _: AdminUser,
) -> ApiResponse[DocumentInfo]:
    """对单份文档重新执行解析、分块、向量化。

    什么时候用：文档处理失败了想重试，或者调整了分块参数想重新切一遍。
    重建是幂等的——旧向量会先被清掉再写新的，不会产生重复数据。
    """
    doc = document_service.prepare_reindex(db, doc_id)
    background_tasks.add_task(document_service.process_document, doc_id)
    return ApiResponse.ok(DocumentInfo.model_validate(doc), message="已开始重建索引")
