"""Chroma 向量库封装。

设计要点：**每个知识库对应一个独立的 collection**（名为 kb_1、kb_2…）。
这样做的三个好处：
  1. 数据天然隔离，检索时不可能串到别的知识库
  2. 删除整个知识库只需 drop 掉一个 collection，干净利落
  3. 每个库的向量规模独立，检索范围小、速度快

另一个关键点是**写入串行化**。SQLite 在 Windows 上并发写入容易报
「database is locked」，而 Chroma 底层用的就是 SQLite。所以所有写操作
都排在一个锁后面依次执行，牺牲一点吞吐换取稳定性——对知识库管理这种
低频写操作来说，这笔买卖很划算。
"""

import asyncio
import logging
import threading

import chromadb
from langchain_chroma import Chroma
from langchain_core.documents import Document

from app.core.config import settings
from app.rag.embeddings import get_embeddings

logger = logging.getLogger(__name__)

# 全局唯一的持久化客户端。Chroma 内部会缓存已打开的 collection，
# 反复新建客户端反而会拖慢速度、还可能引发文件锁冲突。
_client: chromadb.ClientAPI | None = None

# 已实例化的 collection 缓存：kb_id → Chroma 对象
_stores: dict[int, Chroma] = {}

# 写操作互斥锁。注意：它在模块加载时创建，绑定到当时的事件循环。
# FastAPI 全程只有一个事件循环，所以没问题；如果将来改用多进程部署，
# 需要换成基于文件或数据库的分布式锁。
_write_lock = asyncio.Lock()

# 创建客户端/collection 时的互斥锁。
#
# **为什么必须是 threading.Lock 而不是 asyncio.Lock**：
# 检索是两路并行的（向量路和关键词路），其中关键词路跑在线程池里
# （见 pipeline.py 的 asyncio.to_thread），所以 get_store 有可能
# 在两个不同的**操作系统线程**里同时被调用。asyncio.Lock 只在单个事件循环内
# 有效，跨线程用了等于没加锁。
#
# 不加锁会出什么事（这是实际踩到的）：新进程启动后第一次提问时，
# 两个线程同时发现"客户端还没建"，于是各建了一个指向**同一个目录**的
# Chroma 客户端。Chroma 内部按路径管理单例，第二个客户端一建出来
# 就把第一个的 System 停掉了，正在用它的那个线程随即拿到失效的句柄，
# 报出 `'RustBindingsAPI' object has no attribute 'bindings'`
# 和 `KeyError: '...chroma_db'` 这类看起来莫名其妙的错误。
# 加锁之后全局只会创建一个客户端，问题根除。
_init_lock = threading.Lock()


def get_client() -> chromadb.ClientAPI:
    """获取（懒加载的）全局 Chroma 客户端。线程安全。"""
    global _client

    # 先做一次无锁检查：绝大多数调用发生时客户端早就建好了，
    # 这条快路径不用抢锁，避免每次检索都白白排队
    if _client is None:
        with _init_lock:
            # 拿到锁之后要**再检查一次**。
            # 因为等锁的这段时间里，别的线程可能已经把客户端建好了，
            # 不重新检查的话这里会再建一个，等于白加锁
            if _client is None:
                settings.ensure_dirs()
                _client = chromadb.PersistentClient(path=str(settings.chroma_dir))
                logger.info("Chroma 向量库已就绪：%s", settings.chroma_dir)

    return _client


def collection_name(kb_id: int) -> str:
    """知识库 ID → collection 名。"""
    return f"kb_{kb_id}"


def get_store(kb_id: int) -> Chroma:
    """获取某个知识库对应的向量库对象。线程安全。

    用了和 get_client 一样的「无锁快路径 + 加锁慢路径 + 二次检查」写法。
    这里同样不能省锁：collection 是在第一次用到某个知识库时才创建的
    （惰性加载），而"第一次用到"往往正是两路检索同时开工的那一刻。
    """
    store = _stores.get(kb_id)
    if store is not None:
        return store

    # **注意这里的顺序，不能改**：先把客户端拿到手（get_client 内部自己有锁），
    # 再进临界区建 collection。
    #
    # 如果写成"持锁期间调用 get_client()"，会**死锁**：
    # threading.Lock 不可重入，而首次运行时 _client 还是 None，
    # get_client() 也会去抢同一把锁，于是当前线程抱着锁等自己释放锁，永远等下去。
    # 服务表现为"第一次提问就卡住，不报错也不返回"，极难排查。
    # （这个坑实际踩过：单元测试没发现，因为测试夹具预先设好了 _client，
    #   get_client 走的是不抢锁的快路径。）
    client = get_client()

    with _init_lock:
        # 二次检查：等锁期间别的线程可能已经建好了
        store = _stores.get(kb_id)
        if store is None:
            store = Chroma(
                client=client,
                collection_name=collection_name(kb_id),
                embedding_function=get_embeddings(),
                # 用余弦距离衡量相似度。文本向量更关注「方向」而不是「长度」，
                # 余弦距离比默认的欧氏距离更适合语义检索。
                collection_metadata={"hnsw:space": "cosine"},
            )
            _stores[kb_id] = store

    return store


def _forget_store(kb_id: int) -> None:
    """把缓存里的 collection 对象丢掉（知识库被删除时调用）。"""
    # 加锁是为了和 get_store 的写入互斥：不加的话可能刚好在
    # get_store 重新塞进缓存之后才 pop，等于白清了
    with _init_lock:
        _stores.pop(kb_id, None)


def _is_collection_missing(error: Exception) -> bool:
    """判断异常是不是「collection 不存在」。

    同时按类型和消息文本判断：不同版本的 chromadb 抛的异常类型不完全一致，
    只认类型会漏掉一些情况；只认文本又太脆弱，所以两个条件满足其一就算。
    """
    try:
        import chromadb.errors as chroma_errors

        if isinstance(error, chroma_errors.NotFoundError):
            return True
    except (ImportError, AttributeError):
        pass

    return "does not exist" in str(error).lower()


def _with_reconnect(kb_id: int, operation):
    """执行向量库操作，遇到「collection 不存在」就清缓存重试一次。

    为什么需要这个：get_store() 会把 Chroma 的 collection 句柄缓存起来复用
    （每次重新打开开销不小）。但如果这个 collection 在别处被删掉了——
    比如管理员在另一个浏览器标签删了知识库、有人手工清理过数据、
    或者开发时跑过程序之外的脚本——缓存里那个句柄就失效了。

    失效之后所有操作都会持续报 NotFoundError，而且**不会自己恢复**，
    非得重启服务不可。这个函数在捕获到该错误后清掉缓存并重试，
    重试时 get_store 会重新创建 collection，服务就自愈了。

    operation 是一个无参函数，里面写真正的操作。
    """
    try:
        return operation()
    except Exception as e:
        if not _is_collection_missing(e):
            raise  # 其它错误（网络、磁盘、权限）不在这里处理

        logger.warning(
            "知识库 %s 的向量集合已失效（可能是被其它进程删除），清除缓存后重试", kb_id
        )
        _forget_store(kb_id)
        return operation()


# ============================================================
# 写操作（全部串行执行）
# ============================================================
def _add_chunks_sync(kb_id: int, docs: list[Document], start_index: int = 0) -> int:
    """把分块写入向量库（同步实现，由下面的异步函数调度）。"""
    if not docs:
        return 0

    # 给每个分块生成稳定且唯一的 ID。
    # 格式：doc{文档ID}_c{块序号}。稳定的意思是：同一个文档重建索引时
    # 生成的 ID 不变，这样重新写入会自动覆盖旧数据，不会产生重复。
    ids = [f"doc{d.metadata.get('doc_id', 0)}_c{start_index + i}" for i, d in enumerate(docs)]

    # 顺手把 chunk_id 写进元数据。
    # 为什么需要：检索回来的 Document 只带 metadata，拿不到向量库里的 ID。
    # 而引用溯源要记录「这条引用对应哪个分块」——将来做点踩反馈分析、
    # 或者想知道某段内容被引用过多少次，都得靠这个 ID 才能关联回去。
    for doc, chunk_id in zip(docs, ids, strict=True):
        doc.metadata["chunk_id"] = chunk_id

    def _do() -> int:
        store = get_store(kb_id)
        # 先把已有的同 ID 记录删掉，再写入，保证「重建索引」是幂等的
        store.delete(ids=ids)
        store.add_documents(documents=docs, ids=ids)
        return len(docs)

    written = _with_reconnect(kb_id, _do)
    logger.info("知识库 %s 写入 %d 个分块", kb_id, written)
    return written


async def add_chunks(kb_id: int, docs: list[Document], start_index: int = 0) -> int:
    """把一个文档的所有分块写进向量库。

    加锁保证同一时刻只有一个写操作在跑；写入本身是同步阻塞调用，
    丢进线程池执行，免得卡住事件循环。
    """
    async with _write_lock:
        return await asyncio.to_thread(_add_chunks_sync, kb_id, docs, start_index)


def _delete_by_doc_sync(kb_id: int, doc_id: int) -> None:
    """删除某个文档的全部向量（同步实现）。

    集合不存在时不算错误——本来就没有要删的东西，
    _with_reconnect 会重建一个空集合，结果是一样的。
    """
    _with_reconnect(kb_id, lambda: get_store(kb_id).delete(where={"doc_id": doc_id}))
    logger.info("知识库 %s 已清除文档 %s 的向量", kb_id, doc_id)


async def delete_by_doc(kb_id: int, doc_id: int) -> None:
    """删除某个文档的全部向量。"""
    async with _write_lock:
        await asyncio.to_thread(_delete_by_doc_sync, kb_id, doc_id)


def _drop_kb_sync(kb_id: int) -> None:
    """彻底删除一个知识库的向量数据（同步实现）。

    删之前先确认 collection 真的存在，否则 Chroma 会抛异常。
    """
    client = get_client()
    name = collection_name(kb_id)
    try:
        existing = {c if isinstance(c, str) else c.name for c in client.list_collections()}
        if name in existing:
            client.delete_collection(name)
            logger.info("已删除知识库 %s 的向量集合", kb_id)
    except Exception as e:
        # 删知识库时向量清理失败不该阻断整个删除流程（数据库记录还是要删的），
        # 记下日志、人工排查即可
        logger.warning("删除知识库 %s 的向量集合时出错：%s", kb_id, e)
    finally:
        _forget_store(kb_id)


async def drop_kb(kb_id: int) -> None:
    """彻底删除一个知识库的向量数据。"""
    async with _write_lock:
        await asyncio.to_thread(_drop_kb_sync, kb_id)


# ============================================================
# 读操作（不需要加锁，可以并发）
# ============================================================
def search(kb_id: int, query: str, k: int = 5) -> list[tuple[Document, float]]:
    """在单个知识库里检索最相关的 k 个分块。

    返回 (分块, 相似度) 的列表，相似度是 0~1 之间的小数，越大越相关。

    注意单位的换算：因为我们把 collection 设成了余弦距离，
    Chroma 返回的是「距离」（0 表示完全相同，2 表示完全相反）。
    这里统一换算成「相似度 = 1 - 距离」，对用户和下游逻辑都更直观——
    说"相关度 0.87"比说"距离 0.13"好懂得多。
    """
    def _do():
        return get_store(kb_id).similarity_search_with_score(query, k=k)

    results = _with_reconnect(kb_id, _do)
    return [(doc, 1.0 - float(distance)) for doc, distance in results]


async def search_async(kb_id: int, query: str, k: int = 5) -> list[tuple[Document, float]]:
    """search 的异步版。检索要走网络请求算查询向量，别卡住事件循环。"""
    return await asyncio.to_thread(search, kb_id, query, k)


def _dedupe_by_content(
    results: list[tuple[Document, float]],
) -> list[tuple[Document, float]]:
    """按内容去重，同一段内容只保留相关度最高的那一条。

    为什么需要：同一份商品资料可能被放进多个知识库
    （比如「手机数码」和「促销活动」两个库都收录了同一款手机）。
    用户选择「全部知识库」提问时，这段内容会被每个库各返回一次，
    结果就是前 5 条里有一半是重复的，白白浪费了名额——
    本来能带 5 个不同角度的资料，实际只带了 2~3 个。

    按内容而不是按 chunk_id 去重：不同知识库里的分块 ID 是各自独立生成的
    （格式是 doc{文档ID}_c{序号}，文档 ID 不同），同一个内容在两边的
    ID 完全不一样，按 ID 判断不出重复。
    """
    if len(results) <= 1:
        return results

    best: dict[int, tuple[Document, float]] = {}
    for doc, score in results:
        key = hash(doc.page_content)
        existing = best.get(key)
        if existing is None or score > existing[1]:
            best[key] = (doc, score)

    deduped = list(best.values())
    deduped.sort(key=lambda pair: pair[1], reverse=True)
    return deduped


async def search_multi(kb_ids: list[int], query: str, k: int = 5) -> list[tuple[Document, float]]:
    """在多个知识库里同时检索，合并结果。

    用途：用户提问时选择「全部知识库」。
    各库并行检索，合并后按相关度排序取前 k 个，并做内容去重。
    """
    if not kb_ids:
        return []
    if len(kb_ids) == 1:
        return await search_async(kb_ids[0], query, k)

    # 每个库都多取一些，合并后再截断。多取是有必要的：
    # 去重会损耗一部分结果，而且某个库的好结果可能被另一个库的
    # 中等结果挤掉，多留点余量最后再筛更稳妥
    per_kb_k = max(k * 2, 10)
    results_per_kb = await asyncio.gather(
        *(search_async(kb_id, query, per_kb_k) for kb_id in kb_ids),
        return_exceptions=True,
    )

    merged: list[tuple[Document, float]] = []
    errors: list[Exception] = []

    for kb_id, result in zip(kb_ids, results_per_kb, strict=True):
        if isinstance(result, Exception):
            # 单个库检索失败（比如刚被删除、索引损坏）不该让整个问答挂掉，
            # 记下来继续处理其它库
            logger.warning("知识库 %s 检索失败：%s", kb_id, result)
            errors.append(result)
            continue
        merged.extend(result)

    # 关键：如果**所有**库都失败了，要把异常抛出去，不能假装"没搜到"。
    # 「没有相关资料」和「检索功能坏了」是完全不同的两种情况：
    #   前者应该告诉用户"资料里没有"，后者应该报错提示重试。
    # 都返回空列表的话，用户会以为知识库里真的没这份资料，
    # 而实际上是系统故障——这种静默失败最难排查。
    if errors and len(errors) == len(kb_ids):
        raise errors[0]

    return _dedupe_by_content(merged)[:k]


def get_chunks(
    kb_id: int,
    doc_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """读取分块内容。管理页预览分块、以及重建索引时都要用。

    doc_id 传了就只看那份文档的分块，不传就返回整个知识库的分块。
    """
    where = {"doc_id": doc_id} if doc_id is not None else None
    return _with_reconnect(
        kb_id, lambda: get_store(kb_id).get(where=where, limit=limit, offset=offset)
    )


def count(kb_id: int) -> int:
    """统计某个知识库里的分块总数。

    出错时返回 0 而不是抛异常：这个数字只是界面上显示用，
    取不到不该让整个管理页崩掉。但**必须记日志**——
    之前这里静默吞掉所有异常，结果掩盖了「向量库被删空」的真问题，
    排查了很久才找到。错误信息不能藏。
    """
    try:
        return _with_reconnect(kb_id, lambda: get_store(kb_id)._collection.count())
    except Exception as e:
        logger.warning("统计知识库 %s 的分块数失败：%s", kb_id, e)
        return 0


def get_all_chunks(kb_id: int, limit: int = 10000) -> list[Document]:
    """取出整个知识库的所有分块。

    用途：构建 BM25 关键词索引时需要一个库的全量文本。
    数量上限是为了防止知识库特别大时把内存撑爆；
    真到那个规模，应该换成 Lucene 这类专业的全文检索引擎。
    """
    result = _with_reconnect(
        kb_id, lambda: get_store(kb_id).get(limit=limit, include=["documents", "metadatas"])
    )

    docs: list[Document] = []
    for content, metadata in zip(
        result.get("documents") or [], result.get("metadatas") or [], strict=True
    ):
        if content:
            docs.append(Document(page_content=content, metadata=metadata or {}))
    return docs
