"""文档业务逻辑：上传 → 解析 → 分块 → 向量化 → 入库。

整个流程是**异步后台执行**的：用户点上传后接口立刻返回，文档先进入
「处理中」状态，后台慢慢跑解析和向量化，前端轮询状态显示进度。
为什么这样做：一份几十页的 PDF 解析加向量化要几十秒，如果同步等待，
用户会以为页面卡死了，而且这段时间连接一直占着，并发能力也会变差。
"""

import asyncio
import hashlib
import logging
import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError
from sqlalchemy.orm import Session

from app.core import cache as answer_cache
from app.core.config import settings
from app.db.base import SessionLocal
from app.models.document import Document, DocumentStatus
from app.models.knowledge_base import KnowledgeBase
from app.rag import bm25, vectorstore
from app.rag.loaders import UnsupportedFileTypeError, load_document
from app.rag.splitter import split_documents
from app.services import kb_service
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# 文件名里不允许出现的字符（路径分隔符、Windows 保留字符等）。
# 必须过滤掉：用户可以把文件命名成 "../../../windows/system32/x.txt"，
# 如果直接拿来拼路径，文件就写到系统目录去了（路径穿越攻击）。
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(filename: str) -> str:
    """把用户上传的文件名洗成一个安全的文件名。

    做三件事：
      1. 只取最后一段，去掉任何目录部分（挡住 ../../ 这类路径穿越）
      2. 替换掉 Windows 不允许的字符
      3. 限制长度，避免超出文件系统上限
    """
    # Path(...).name 在 Windows 上能正确切掉 C:\a\b\ 和 ../../ 两种前缀
    name = Path(filename.replace("\\", "/")).name
    name = _UNSAFE_FILENAME_CHARS.sub("_", name).strip(" .")

    if not name:
        name = "unnamed"

    # 保留扩展名的情况下截断主名，总长控制在 100 字符内
    suffix = Path(name).suffix
    stem = Path(name).stem[: 100 - len(suffix)]
    return f"{stem}{suffix}"


def compute_file_hash(content: bytes) -> str:
    """算文件内容的 SHA-256 指纹，用于判断是不是同一个文件。"""
    return hashlib.sha256(content).hexdigest()


# ============================================================
# 上传
# ============================================================
def upload_document(
    db: Session,
    kb_id: int,
    filename: str,
    content: bytes,
    user_id: int | None,
) -> tuple[Document, str]:
    """保存上传的文件并建记录。

    返回 (文档记录, 处理结果)，处理结果有三种：
        "created"    —— 新文件，已入库，等后台处理
        "duplicated" —— 内容完全相同的文件之前传过，直接复用已有记录
    """
    kb = kb_service.get_knowledge_base(db, kb_id)

    safe_name = sanitize_filename(filename)
    suffix = Path(safe_name).suffix.lower()

    # 类型校验放这里而不是接口层，是为了让「文件名」这个参数只被解析一次，
    # 避免接口层和这里对文件名的理解不一致（比如接口层没做 sanitize）
    if suffix not in settings.allowed_extension_set:
        supported = "、".join(sorted(settings.allowed_extension_set))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"「{safe_name}」的类型不支持，目前支持：{supported}",
        )

    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"「{safe_name}」是空文件"
        )

    file_hash = compute_file_hash(content)

    # 幂等检查：同一个知识库内，内容一样就不重复处理。
    # 为什么用内容哈希而不是文件名判断：同一份资料改个名字再传一次很常见，
    # 只比文件名会重复入库；反过来同名但内容不同的文件也应该能入库。
    #
    # 注意查询条件里带了 kb_id：去重范围是**当前知识库**。
    # 同一份资料允许出现在多个知识库里（商品资料被多个库共用是正常需求），
    # 只在同一个库内防止重复。
    existing = db.scalar(
        select(Document).where(
            Document.kb_id == kb_id, Document.file_hash == file_hash
        )
    )
    if existing is not None:
        logger.info("文件内容重复，跳过处理：%s（已存在于文档 %s）", safe_name, existing.id)
        return existing, "duplicated"

    # 落盘。文件名加 uuid 前缀，避免两个用户传同名文件时互相覆盖
    settings.ensure_dirs()
    stored_name = f"{uuid.uuid4().hex}_{safe_name}"
    stored_path = settings.upload_dir / stored_name
    stored_path.write_bytes(content)

    doc = Document(
        kb_id=kb.id,
        filename=safe_name,  # 展示给用户的是原始文件名，不带 uuid 前缀
        stored_path=str(stored_path),
        file_hash=file_hash,
        file_type=suffix,
        file_size=len(content),
        status=DocumentStatus.PENDING,
        uploaded_by=user_id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)

    kb_service.refresh_stats(db, kb_id)
    logger.info("文档已上传：%s (id=%s, %.1f KB)", safe_name, doc.id, len(content) / 1024)
    return doc, "created"


# ============================================================
# 处理流程（后台执行）
# ============================================================
def _still_exists(db: Session, doc_id: int, kb_id: int) -> bool:
    """回查数据库，确认文档和它所属的知识库都还在。

    必须**重新查一次数据库**，不能拿内存里那个 doc 对象判断——
    别的会话（比如删除接口）把行删掉了，当前会话的身份映射里
    还留着这个对象，读它永远显示"存在"。

    每次查询前的事务都已经 commit 过，所以拿到的是最新数据。
    """
    if db.scalar(select(Document.id).where(Document.id == doc_id)) is None:
        return False
    return db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.id == kb_id)) is not None


class DocumentGoneError(Exception):
    """文档（或它所属的知识库）在处理过程中被删掉了。

    这不是故障，是一种正常的并发情况：管理员在处理期间点了删除。
    单独定义一个异常类型，是为了让处理流程能干净地"到此为止"，
    而不是把它当成错误去写失败状态——**那条记录已经不存在了，写不进去**。
    """


def _set_status(db: Session, doc: Document, new_status: str) -> None:
    """更新文档状态并立即提交，让前端轮询时能实时看到进度。

    每一步都单独提交是有意为之：如果攒到最后一起提交，
    处理过程中前端看到的永远是「处理中」，看不出到底走到哪一步了。

    提交时要防一手"行已经不在了"：处理中途管理员把文档或整个知识库删掉，
    这条 UPDATE 就找不到目标行，SQLAlchemy 会抛 StaleDataError。
    以前这个异常会一路冒到最外层的兜底，触发 _mark_failed，
    而 _mark_failed 里的 commit 同样会失败、再抛一次——**异常就这样逃出了后台任务**。
    """
    # 先把 id 取出来。rollback 之后 doc 对象就失效了，
    # 那时再读 doc.id 会触发一次数据库刷新，而行已经不在了，
    # 于是抛 ObjectDeletedError —— 本来是报错信息，自己却成了新的错误源
    doc_id = doc.id

    doc.status = new_status
    doc.error_msg = None
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        raise DocumentGoneError(f"文档 {doc_id} 已不存在") from None


# 限制同时处理的文档数量。
#
# 为什么必须限制（这是踩过坑才加的）：一份文档的处理过程要几秒钟，
# 期间它一直占着一个数据库连接和一次云端接口调用额度。
# 如果管理员一次性提交几十份文档重建索引，这些后台任务会同时启动，
# 每个都去抢数据库连接——默认连接池只有 15 个，瞬间被打满，
# 后续任务全部报「QueuePool limit reached」超时失败。
#
# 而且失败的表现极具迷惑性：从界面上看只是"文档一直卡在处理中"，
# 完全没有报错，根本想不到是连接池的问题。
#
# 控制并发数之后，无论提交多少文档，同时在跑的始终只有几个，
# 连接数是可控的。
_processing_semaphore = asyncio.Semaphore(settings.DOCUMENT_PROCESS_CONCURRENCY)

# 正在排队或正在处理的文档 ID。
#
# **为什么需要它**：同一份文档有可能被重复提交处理——上传之后立刻点「重建索引」
# （这时状态还是"待处理"）、或者手快连点两次。两个任务同时跑同一份文档会：
#   - 重复调用云端向量化接口，白花钱
#   - 两边都先删旧向量再写新向量，交错执行，最终内容谁都说不准
#   - chunk_count 和日志以最后写入的为准，和实际向量数可能对不上
#
# 这个集合记录了"已经在处理队列里"的文档，重复提交时直接跳过。
# 放在事件循环上操作，检查和添加之间没有 await，所以不会被打断，
# 不需要额外加锁。
_processing_ids: set[int] = set()

# 正在被删除的文档 ID。
# 删除和处理可能同时发生（用户点了删除，而这份文档正好在处理中），
# 两边都要让对方知道自己正在动手，否则会互相踩：
# 处理任务把刚被删掉的文档又写回向量库，而那个知识库可能已经被删了。
_deleting_ids: set[int] = set()


async def process_document(doc_id: int) -> None:
    """完整处理一份文档：解析 → 分块 → 向量化 → 入库。

    这个函数在后台任务里跑，**必须自己创建数据库会话**：
    请求用的那个会话在接口返回时就已经关闭了。

    整个函数包在 try 里，任何一步出错都记进 error_msg 并标记失败状态，
    绝不让异常抛出去——后台任务的异常没人接，会让进程里留下隐患。
    """
    if doc_id in _processing_ids:
        logger.info("文档 %s 已经在处理队列里，跳过这次重复提交", doc_id)
        return
    if doc_id in _deleting_ids:
        logger.info("文档 %s 正在被删除，跳过处理", doc_id)
        return

    _processing_ids.add(doc_id)
    try:
        # 并发限制放在这一层：排队等待期间不占用数据库连接
        async with _processing_semaphore:
            await _process_document_inner(doc_id)
    finally:
        _processing_ids.discard(doc_id)


def is_processing(doc_id: int) -> bool:
    """这份文档是不是正在处理中。删除接口用它决定要不要拦一下。"""
    return doc_id in _processing_ids


async def _process_document_inner(doc_id: int) -> None:
    """文档处理的实际逻辑（由 process_document 控制并发后调用）。"""
    with SessionLocal() as db:
        doc = db.get(Document, doc_id)
        if doc is None:
            logger.warning("文档 %s 不存在，跳过处理", doc_id)
            return

        # 已经处理成功的文档不重复处理（比如重复点了「重建索引」）
        if doc.status == DocumentStatus.READY and doc.chunk_count > 0:
            logger.info("文档 %s 已处理过，跳过", doc_id)
            return

        try:
            # ---------- 第 1 步：解析 ----------
            _set_status(db, doc, DocumentStatus.PARSING)
            parsed = load_document(
                doc.stored_path,
                doc.filename,
                extra_metadata={
                    "doc_id": doc.id,
                    "kb_id": doc.kb_id,
                    "file_type": doc.file_type,
                },
            )
            if not parsed:
                raise ValueError("文件里没有提取到任何内容")

            # ---------- 第 2 步：分块 ----------
            _set_status(db, doc, DocumentStatus.CHUNKING)
            chunks = split_documents(
                parsed,
                chunk_size=settings.CHUNK_SIZE,
                chunk_overlap=settings.CHUNK_OVERLAP,
                add_context=settings.CHUNK_CONTEXT_ENABLED,
            )
            if not chunks:
                raise ValueError("分块后没有产生任何有效内容")

            # ---------- 第 3 步：向量化并写入向量库 ----------
            _set_status(db, doc, DocumentStatus.EMBEDDING)

            # **写向量之前，先回查一下这份文档和它所属的知识库还在不在。**
            #
            # 处理要跑好几秒，这期间管理员完全可能把文档、甚至整个知识库删掉。
            # 如果照写不误，会留下一个极难排查的问题：
            #   Chroma 的 collection 是按知识库 ID 命名的（kb_3），
            #   而 SQLite 的自增 ID **会被复用**——删掉知识库 3 之后新建一个，
            #   新库拿到的还是 ID=3，也就是同一个 collection 名。
            #   于是刚被在途任务写回去的旧向量，就"属于"这个新建的空知识库了：
            #   界面上显示 0 份文档，可用户一提问却能搜出已经删掉的文件内容，
            #   引用里还显示着已删除的文件名。
            #
            # 这种"删了还能搜出来"的现象，光看代码根本想不到是时序问题。
            if not _still_exists(db, doc_id, doc.kb_id):
                logger.warning("文档 %s 在处理过程中被删除（或所属知识库被删），放弃本次处理", doc_id)
                return

            # 重建索引时先清掉旧向量，否则会和新向量混在一起变成两份
            await vectorstore.delete_by_doc(doc.kb_id, doc.id)
            await vectorstore.add_chunks(doc.kb_id, chunks)

            # ---------- 完成 ----------
            doc.status = DocumentStatus.READY
            doc.chunk_count = len(chunks)
            doc.indexed_at = utcnow()
            doc.error_msg = None
            try:
                db.commit()
            except StaleDataError:
                # 上面的存在性检查到这里之间还有个极小的窗口，
                # 文档刚好被删掉了。这条 UPDATE 找不到目标行，SQLAlchemy 会报错。
                # 这不是故障——文档已经不存在了，本来就不该再写状态。
                # 但向量已经写进去了，得清掉，否则就成了搜得到的孤儿数据
                db.rollback()
                logger.warning("文档 %s 在收尾时被删除，清理刚写入的向量", doc_id)
                await vectorstore.delete_by_doc(doc.kb_id, doc_id)
                return

            kb_service.refresh_stats(db, doc.kb_id)

            # 让关键词索引失效，否则检索时用的还是旧索引，
            # 搜不到刚上传的这份文档（索引是惰性重建的，下次检索时才重建）
            bm25.invalidate(doc.kb_id)

            # 问答缓存同样要清掉。不然用户继续问同样的问题，
            # 拿到的还是"上传这份资料之前"的旧答案
            answer_cache.invalidate_answers()

            logger.info("文档处理完成：%s（%d 个分块）", doc.filename, len(chunks))

        except DocumentGoneError:
            # 文档在处理过程中被删了。不是故障，正常收尾就好——
            # 这时候既不该写状态（行都没了），也不该标记失败
            logger.info("文档 %s 在处理过程中被删除，已停止处理", doc_id)

        except UnsupportedFileTypeError as e:
            _mark_failed(db, doc, str(e))
        except Exception as e:
            # 这里要抓住所有异常，包括网络超时、接口限流、文件损坏等等。
            # 只记消息不记堆栈会给排查造成困难，所以同时写日志和数据库。
            logger.exception("处理文档 %s 失败", doc_id)
            _mark_failed(db, doc, f"{type(e).__name__}: {e}")


def _mark_failed(db: Session, doc: Document, message: str) -> None:
    """把文档标记为处理失败，并记录原因。

    同样要防"行已经不在"：如果失败的原因恰好就是文档被删了，
    这里的 commit 会再次抛错，然后**从 _mark_failed 里逃出去**——
    本来是用来兜底的函数，自己却成了新的异常源。
    """
    # 错误信息截断到 500 字：完整堆栈可能几千字，塞进数据库没意义，
    # 详细内容已经写进服务端日志了
    doc_id = doc.id  # 同理：rollback 之后就读不到了

    doc.status = DocumentStatus.FAILED
    doc.error_msg = message[:500]
    try:
        db.commit()
    except StaleDataError:
        db.rollback()
        logger.info("文档 %s 已被删除，无需标记失败状态", doc_id)
        return

    kb_service.refresh_stats(db, doc.kb_id)


def reset_stuck_documents() -> int:
    """把卡在中间状态的文档标记为失败。

    什么时候会出现需要清理的「僵尸文档」：服务重启了、进程被强杀了，
    而当时正好有文档在处理中。这些文档的状态会永远停在「解析中」，
    前端会一直转圈等下去，用户也不知道发生了什么。

    每次服务启动时跑一遍，把它们标成失败，用户就能看到「请重新上传/重建索引」
    的提示，而不是无限等待。
    """
    with SessionLocal() as db:
        stuck = list(
            db.scalars(
                select(Document).where(Document.status.in_(DocumentStatus.IN_PROGRESS))
            ).all()
        )

        for doc in stuck:
            doc.status = DocumentStatus.FAILED
            doc.error_msg = "服务重启导致处理中断，请点击「重建索引」重试"

        if stuck:
            db.commit()
            logger.warning("发现 %d 份文档卡在处理中，已标记为失败", len(stuck))

        return len(stuck)


# ============================================================
# 查询
# ============================================================
def list_documents(
    db: Session,
    kb_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
    status_filter: str | None = None,
    keyword: str | None = None,
) -> tuple[list[Document], int]:
    """分页查询文档列表。管理页的表格数据来源。"""
    stmt = select(Document)

    if kb_id is not None:
        stmt = stmt.where(Document.kb_id == kb_id)
    if status_filter:
        stmt = stmt.where(Document.status == status_filter)
    if keyword:
        stmt = stmt.where(Document.filename.ilike(f"%{keyword}%"))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    stmt = (
        stmt.order_by(Document.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(db.scalars(stmt).all()), total


def get_document(db: Session, doc_id: int) -> Document:
    """按 ID 取文档，不存在就 404。"""
    doc = db.get(Document, doc_id)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="文档不存在")
    return doc


def get_document_chunks(
    db: Session, doc_id: int, limit: int = 50, offset: int = 0
) -> tuple[list[dict], int]:
    """读取某个文档的分块内容，供管理页预览。

    分块正文存在向量库里而不是数据库里，所以这里要去查 Chroma。
    """
    doc = get_document(db, doc_id)

    if doc.status != DocumentStatus.READY:
        return [], 0

    result = vectorstore.get_chunks(doc.kb_id, doc_id=doc.id, limit=limit, offset=offset)
    ids = result.get("ids") or []
    documents = result.get("documents") or []
    metadatas = result.get("metadatas") or []

    items = [
        {"id": chunk_id, "content": content, "metadata": metadata or {}}
        for chunk_id, content, metadata in zip(ids, documents, metadatas, strict=False)
    ]
    return items, doc.chunk_count


# ============================================================
# 删除与重建
# ============================================================
async def delete_document(db: Session, doc_id: int) -> None:
    """删除文档：清除它的向量、磁盘文件，以及数据库记录。

    同样的顺序原则：先删向量，再删记录。
    向量删失败只是留点垃圾，记录删了向量却还在的话，检索时会捞到
    已经"不存在"的内容，用户点引用会看到空白的来源。
    """
    doc = get_document(db, doc_id)
    kb_id = doc.kb_id
    filename = doc.filename
    stored_path = doc.stored_path

    # 正在处理的文档先不让删。为什么：
    # 后台任务还在往向量库写这份文档的分块，这边同时删，两边会互相踩——
    # 删完之后任务又把向量写回去，变成删不掉的孤儿数据。
    # 与其让它俩赛跑，不如明确告诉用户等一下（几秒钟的事）。
    if is_processing(doc_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="这份文档正在处理中，请等它处理完再删除",
        )

    # 登记一下"我正在删这份文档"，让期间任何新启动的处理任务直接跳过
    _deleting_ids.add(doc_id)
    try:
        await _delete_document_inner(db, doc, kb_id, stored_path, filename)
    finally:
        _deleting_ids.discard(doc_id)


async def _delete_document_inner(
    db: Session, doc: Document, kb_id: int, stored_path: str, filename: str
) -> None:
    """删除的实际动作（由 delete_document 登记完成后调用）。"""
    doc_id = doc.id  # 删掉记录之后就取不到了，先存下来

    await vectorstore.delete_by_doc(kb_id, doc.id)

    db.delete(doc)
    db.commit()

    # 磁盘上的原文件也删掉，不然上传目录会越堆越大
    try:
        Path(stored_path).unlink(missing_ok=True)
    except OSError as e:
        # 文件被占用、权限不足等情况删不掉。不影响功能，记个日志就行
        logger.warning("删除文件 %s 失败：%s", stored_path, e)

    kb_service.refresh_stats(db, kb_id)

    # 同理：文档删了，关键词索引里还留着它的内容，
    # 不失效的话会被检索出来，用户点引用却找不到原文
    bm25.invalidate(kb_id)

    # 缓存里可能还存着"引用了这份已删文档"的答案，必须清掉，
    # 否则用户会看到指向不存在文档的引用
    answer_cache.invalidate_answers()

    logger.info("已删除文档：%s (id=%s)", filename, doc_id)


def prepare_reindex(db: Session, doc_id: int) -> Document:
    """把文档重置为「待处理」，交给后台重新跑一遍完整流程。

    什么场景会用到：文档处理失败了想重试、改了分块参数想重新切、
    或者向量库数据损坏了要重建。
    """
    doc = get_document(db, doc_id)

    # 检查原文件还在不在。如果被误删了就重建不了，早点报错比后台默默失败好
    if not Path(doc.stored_path).exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="原始文件已丢失，无法重建索引，请删除后重新上传",
        )

    doc.status = DocumentStatus.PENDING
    doc.chunk_count = 0
    doc.error_msg = None
    doc.indexed_at = None
    db.commit()
    db.refresh(doc)
    return doc


def get_kb_status_summary(db: Session, kb_id: int) -> dict:
    """统计某个知识库里各种状态的文档数量。

    管理页用它决定要不要继续轮询：只要还有文档在处理中，
    前端就每两秒刷新一次；全都处理完了就停止轮询，不做无谓的请求。
    """
    rows = db.execute(
        select(Document.status, func.count())
        .where(Document.kb_id == kb_id)
        .group_by(Document.status)
    ).all()

    by_status = {status_value: count for status_value, count in rows}
    in_progress = sum(by_status.get(s, 0) for s in DocumentStatus.IN_PROGRESS)

    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "in_progress": in_progress,
        "has_running_tasks": in_progress > 0,
    }


def ensure_kb_has_ready_document(db: Session, kb_id: int) -> KnowledgeBase:
    """确认知识库里至少有一份处理成功的文档。问答前校验用。"""
    kb = kb_service.get_knowledge_base(db, kb_id)
    ready_count = (
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.kb_id == kb_id, Document.status == DocumentStatus.READY)
        )
        or 0
    )
    if ready_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"知识库「{kb.name}」里还没有可用的资料，请先上传文档",
        )
    return kb
