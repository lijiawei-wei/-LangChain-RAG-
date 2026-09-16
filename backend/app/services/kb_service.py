"""知识库业务逻辑。"""

import logging
from pathlib import Path

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import cache as answer_cache
from app.models.document import Document
from app.models.knowledge_base import KnowledgeBase
from app.rag import bm25, vectorstore

logger = logging.getLogger(__name__)


def get_knowledge_base(db: Session, kb_id: int) -> KnowledgeBase:
    """按 ID 取知识库，不存在就返回 404。

    抽成独立函数是因为几乎所有文档接口都要先用它校验「这个知识库存在吗」，
    写一遍就够了。
    """
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="知识库不存在"
        )
    return kb


def list_knowledge_bases(
    db: Session,
    page: int = 1,
    page_size: int = 20,
    keyword: str | None = None,
) -> tuple[list[KnowledgeBase], int]:
    """分页查询知识库列表。

    返回 (当前页数据, 总条数)。
    """
    stmt = select(KnowledgeBase)

    if keyword:
        # ilike 是大小写不敏感的模糊匹配
        stmt = stmt.where(KnowledgeBase.name.ilike(f"%{keyword}%"))

    # 先查总数（分页组件要显示"共 N 条"）
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    # 按更新时间倒序：最近动过的排最前面，符合使用习惯
    stmt = (
        stmt.order_by(KnowledgeBase.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = list(db.scalars(stmt).all())
    return items, total


def create_knowledge_base(
    db: Session, name: str, description: str | None, user_id: int | None
) -> KnowledgeBase:
    """新建知识库。

    名称不允许重复：重名的知识库在界面上很难区分，
    而且用户选择知识库提问时会搞不清该选哪个。
    """
    existing = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"已经有一个叫「{name}」的知识库了，请换个名字",
        )

    kb = KnowledgeBase(name=name, description=description, created_by=user_id)
    db.add(kb)
    db.commit()
    db.refresh(kb)
    logger.info("创建知识库：%s (id=%s)", name, kb.id)
    return kb


def update_knowledge_base(
    db: Session, kb_id: int, name: str | None, description: str | None
) -> KnowledgeBase:
    """重命名或修改描述。"""
    kb = get_knowledge_base(db, kb_id)

    if name is not None and name != kb.name:
        # 改名时也要查重，否则会撞上唯一约束、抛出一个用户看不懂的数据库错误
        conflict = db.scalar(
            select(KnowledgeBase).where(
                KnowledgeBase.name == name, KnowledgeBase.id != kb_id
            )
        )
        if conflict is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"已经有一个叫「{name}」的知识库了，请换个名字",
            )
        kb.name = name

    if description is not None:
        kb.description = description

    db.commit()
    db.refresh(kb)
    return kb


async def delete_knowledge_base(db: Session, kb_id: int) -> None:
    """删除知识库，连同它的文档记录和全部向量。

    顺序很重要：先删向量库，再删数据库记录。
    反过来的话，如果删向量时出错，数据库记录已经没了，
    向量就成了没人认领的垃圾数据，永久残留在磁盘上。
    """
    # 延迟导入：document_service 在模块顶层导入了本模块，
    # 这里如果也写在顶层就成循环导入了。放在函数里按需导入即可。
    from app.services import document_service

    kb = get_knowledge_base(db, kb_id)
    docs = list(db.scalars(select(Document).where(Document.kb_id == kb_id)).all())

    # 库下面还有文档在后台处理时先不让删。
    #
    # **这是必须拦的**：后台任务处理完会把向量写回这个库的 collection，
    # 而 collection 是按知识库 ID 命名的（kb_3），SQLite 的自增 ID **会被复用**——
    # 删掉知识库 3 之后再新建一个，新库同样叫 kb_3。
    # 于是那个还在跑的任务写回去的旧向量，就"属于"这个全新的空库了：
    # 界面上显示 0 份文档，用户一提问却能搜出已经删掉的文件内容。
    #
    # 与其产生这种查都没法查的数据错乱，不如让管理员多等几秒。
    processing = [d.id for d in docs if document_service.is_processing(d.id)]
    if processing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"这个知识库里还有 {len(processing)} 份文档正在处理中，"
                "请等它们处理完再删除知识库"
            ),
        )

    # 把库下面所有文档的原始文件也删掉。
    # 之前只删数据库记录，磁盘上的文件会永远留着——实测跑下来
    # 上传目录里堆了两百多个再也没人认领的文件
    for doc in docs:
        try:
            Path(doc.stored_path).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("删除文件 %s 失败：%s", doc.stored_path, e)

    await vectorstore.drop_kb(kb_id)  # 删 Chroma collection
    bm25.invalidate(kb_id)  # 关键词索引也一起清掉，释放内存
    answer_cache.invalidate_answers()  # 问答缓存同理

    # documents 表配置了级联删除，删知识库会自动带走它下面的文档记录
    db.delete(kb)
    db.commit()
    logger.info("已删除知识库：%s (id=%s)", kb.name, kb_id)


def refresh_stats(db: Session, kb_id: int) -> None:
    """重新统计知识库下的文档数和分块数。

    为什么用「重新统计」而不是「加一减一」：加减法在并发或异常中断时
    很容易算错且难以察觉，时间一长界面上的数字就和实际对不上了。
    重算虽然多一次查询，但结果永远准确，这种情况值得。
    """
    kb = db.get(KnowledgeBase, kb_id)
    if kb is None:
        return

    doc_count = (
        db.scalar(
            select(func.count())
            .select_from(Document)
            .where(Document.kb_id == kb_id)
        )
        or 0
    )
    # 只统计处理成功的文档，失败的不算
    chunk_count = (
        db.scalar(
            select(func.coalesce(func.sum(Document.chunk_count), 0)).where(
                Document.kb_id == kb_id
            )
        )
        or 0
    )

    kb.doc_count = doc_count
    kb.chunk_count = int(chunk_count)
    db.commit()


def list_all_for_select(db: Session) -> list[KnowledgeBase]:
    """取出所有知识库，供问答页的知识库下拉框使用。"""
    return list(db.scalars(select(KnowledgeBase).order_by(KnowledgeBase.name)).all())
