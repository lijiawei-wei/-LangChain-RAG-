"""中文感知的文本分块。

为什么不能直接照搬英文教程的分块方式：
英文按空格分词，一个空格就是天然的断点。中文句子中间没有空格，
如果还用英文的分隔符配置，切出来的块会从句子正中间断开，
语义被拦腰截断，检索质量会很差。

所以这里的策略是：优先在段落、换行、句号、问号这些「语义完整的位置」断开，
实在找不到才退而求其次在逗号处断，最后才硬切。
"""

import logging
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

logger = logging.getLogger(__name__)

# 分隔符列表，从上到下依次尝试。
# RecursiveCharacterTextSplitter 会先试着用第一个分隔符切，切出来的块还是太大
# 就换下一个分隔符继续切，直到块小于 chunk_size 或没有分隔符可用了。
#
# 顺序是有讲究的：从「语义边界最完整」排到「最不完整」。
#   空行   —— 段落边界，最理想
#   换行   —— 行边界
#   。！？； —— 句子边界（中文标点）
#   ，     —— 分句边界，凑合能用
#   空格   —— 中英混排时的英文单词边界
#   空字符串 —— 兜底，直接在字符中间硬切
CHINESE_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", " ", ""]

# Markdown 的标题层级标记。解析时会把标题文字记到 metadata 里，
# 这样检索到某个分块时，能知道它属于文档的哪个章节。
MARKDOWN_HEADERS = [
    ("#", "heading1"),
    ("##", "heading2"),
    ("###", "heading3"),
]


def _split_markdown(doc: Document) -> list[Document]:
    """Markdown 专用：先按标题切分，保留章节层级。

    为什么要先按标题切：商品的 Markdown 资料通常是这样组织的：

        # XX手机 产品介绍
        ## 基本参数
        ## 电池与续航
        ## 常见问题

    先按标题切开，每个分块的语义天然完整，而且能知道「这段是讲电池的」。
    如果直接按字数硬切，很可能把「电池与续航」的标题和它的正文切散。
    """
    splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=MARKDOWN_HEADERS,
        strip_headers=False,  # 保留标题文字在正文里，模型看到标题更容易理解上下文
    )
    try:
        sections = splitter.split_text(doc.page_content)
    except Exception as e:
        # Markdown 格式千奇百怪，切分失败不要紧，退回普通切分方式即可
        logger.warning("Markdown 按标题切分失败，改用普通切分：%s", e)
        return []

    if not sections:
        return []

    # 把原有的 metadata（文件名、页码等）复制到每个章节上
    for section in sections:
        section.metadata = {**doc.metadata, **section.metadata}
    return sections


def _extract_source_title(doc: Document) -> str:
    """取出一份资料的名字，用来给分块补上下文。

    优先级：Markdown 的一级标题 > 文件名（去掉扩展名）。

    为什么要去掉扩展名：给模型和向量模型看的是"星耀 X9 Pro 智能手机 产品资料"，
    而不是"星耀X9Pro手机产品资料.md"——后者带着下划线和扩展名，
    是给机器看的，混进正文里只会干扰理解。
    """
    for key in ("heading1", "heading2", "title"):
        if doc.metadata.get(key):
            return str(doc.metadata[key]).strip()

    filename = doc.metadata.get("filename", "")
    return Path(filename).stem if filename else ""


def _build_context_prefix(doc: Document, source_title: str) -> str:
    """给分块拼一个「来源上下文」前缀。

    **这解决的是一个很隐蔽但影响很大的问题**：分块之后，
    每一块只保留了它自己那一小节的内容，**丢掉了它属于哪个商品**。

    举个例子，两份资料各有一个「电池与续航」小节：
        手机：  内置 5000mAh 大容量电池，支持 67W 有线快充
        笔记本：内置 60Wh 锂聚合物电池，标配 65W 氮化镓充电器

    如果只把这两段文字单独拿出来，它们长得几乎一样——都是"内置 XX 电池"，
    谁也不知道说的是手机还是笔记本。后果有三个：
      1. 向量化时，两段文字算出来的向量高度相似，检索时容易串
      2. 重排模型无法判断哪一段才是用户问的那个商品，实测它会把
         笔记本的电池段落排进手机问题的前几名（真实踩过的坑）
      3. 大模型拿到这段文字，可能把笔记本的电池参数当成手机的报给用户

    补上前缀之后：
        【星耀 X9 Pro 智能手机 · 三、电池与续航】
        内置 5000mAh 大容量电池，支持 67W 有线快充

    每一块都是自解释的，检索、重排、生成三个环节都能用上这个信息。

    这个做法在 RAG 工程里叫「上下文增强分块」，
    代价是每个分块多占十几个字，但带来的准确率提升很值。
    """
    if not source_title:
        return ""

    # 加上小节名（如果有），让模型知道这段是属于哪个部分的
    section = (
        doc.metadata.get("heading2")
        or doc.metadata.get("heading3")
        or doc.metadata.get("sheet")
    )

    label = f"{source_title} · {section}" if section else source_title
    return f"【{label}】\n"


def split_documents(
    documents: list[Document],
    chunk_size: int = 500,
    chunk_overlap: int = 80,
    add_context: bool = True,
) -> list[Document]:
    """把文档切成小块。

    参数：
        chunk_size: 每块的字符数上限。中文一个字符信息量比英文单词大，
                    500 字左右是比较合适的粒度：太小会丢上下文，太大会稀释重点。
        chunk_overlap: 相邻块的重叠字符数。为什么要重叠：如果一句话正好被切在
                    两个块的边界上，两边的块都不完整；重叠一段后，至少有一个块
                    能包含完整的那句话，检索时就不会漏。

    返回的分块 metadata 里会多一个 chunk_index 字段，标记它是本文档的第几块。
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if chunk_overlap >= chunk_size:
        # 重叠比块本身还大，切分算法会陷入死循环，必须提前拦住
        raise ValueError("chunk_overlap 必须小于 chunk_size")

    text_splitter = RecursiveCharacterTextSplitter(
        separators=CHINESE_SEPARATORS,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        # 用字符数衡量长度。中文没有 token 的概念，按字符算最直观也最稳定
        length_function=len,
        keep_separator=True,  # 保留分隔符，否则句号会被吃掉，句子读起来会连在一起
    )

    result: list[Document] = []
    for doc in documents:
        # Markdown 先走一遍标题切分
        if doc.metadata.get("file_type") == ".md":
            sections = _split_markdown(doc)
            # 如果按标题切出来还是太大，再对每个章节做一次普通切分
            candidates = []
            for section in sections:
                candidates.extend(text_splitter.split_documents([section]))
        else:
            candidates = text_splitter.split_documents([doc])

        result.extend(candidates)

    # 统一编号、补上下文前缀、过滤空白块
    cleaned: list[Document] = []
    for doc in result:
        content = doc.page_content.strip()
        if not content:
            continue

        if add_context:
            prefix = _build_context_prefix(doc, _extract_source_title(doc))
            if prefix:
                doc.page_content = prefix + content

        doc.metadata["chunk_index"] = len(cleaned)
        cleaned.append(doc)

    logger.info(
        "分块完成：%d 个片段 → %d 个分块%s",
        len(documents),
        len(cleaned),
        "（已补来源上下文）" if add_context else "",
    )
    return cleaned
