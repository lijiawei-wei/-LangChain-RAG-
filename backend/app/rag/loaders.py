"""文档解析：把各种格式的文件读成 LangChain 的 Document 列表。

支持 PDF、Word(docx)、Excel(xlsx)、纯文本(txt)、Markdown(md)。
每种格式解析出来都带上「页码」「工作表」这类定位信息，
这样后面回答问题时才能告诉用户「答案来自第 3 页」。
"""

import logging
from pathlib import Path

from langchain_core.documents import Document

logger = logging.getLogger(__name__)


class UnsupportedFileTypeError(ValueError):
    """文件类型不支持时抛这个，调用方好识别。"""


# ============================================================
# 各格式的解析实现
# ============================================================
def _load_pdf(path: Path) -> list[Document]:
    """解析 PDF，逐页提取文字。

    注意：必须用 `import pymupdf` 而不是老的 `import fitz`。
    PyMuPDF 从 1.24 起就把 fitz 这个名字标记为废弃了，用它会打警告。
    """
    import pymupdf

    docs: list[Document] = []
    with pymupdf.open(path) as pdf:
        for page_index, page in enumerate(pdf, start=1):
            text = page.get_text().strip()
            if not text:
                # 扫描件或纯图片页提取不到文字，跳过。
                # 说明：这里不做 OCR，如果知识库里有扫描件需要单独处理。
                continue
            docs.append(
                Document(
                    page_content=text,
                    # 页码用 1 开始，符合普通人的阅读习惯（程序员习惯从 0 开始，
                    # 但这里要展示给用户看）
                    metadata={"page": page_index, "total_pages": pdf.page_count},
                )
            )

    if not docs:
        raise ValueError(
            "这个 PDF 里没有提取到任何文字。"
            "如果它是扫描件或纯图片，需要先做 OCR 转换才能入库。"
        )
    return docs


def _load_docx(path: Path) -> list[Document]:
    """解析 Word 文档，提取正文段落和表格。

    表格要单独处理：商品资料里经常用表格列规格参数（尺寸、重量、电池容量…），
    这些内容按段落读是读不到的。
    """
    from docx import Document as DocxDocument

    docx = DocxDocument(str(path))
    blocks: list[str] = []

    # 按文档里的实际顺序遍历段落和表格，保持原有阅读顺序
    for block in docx.iter_inner_content():
        if hasattr(block, "rows"):
            # 是表格
            table_text = _table_to_text(block.rows)
            if table_text:
                blocks.append(table_text)
        else:
            text = block.text.strip()
            if text:
                blocks.append(text)

    content = "\n\n".join(blocks).strip()
    if not content:
        raise ValueError("这个 Word 文档里没有提取到任何内容")

    return [Document(page_content=content, metadata={})]


def _table_to_text(rows) -> str:
    """把表格转成文字。

    商品规格表就这样转：
        参数名 | 参数值
        电池容量 | 5000mAh
    变成：
        电池容量: 5000mAh

    为什么转成「字段: 值」而不是保留表格：文字形式更利于检索模型理解，
    用户问「电池多大」时更容易命中。
    """
    lines: list[str] = []
    for row in rows:
        cells = [cell.text.strip() for cell in row.cells]
        # 去掉全空的行
        if not any(cells):
            continue

        if len(cells) == 2 and cells[0] and cells[1]:
            lines.append(f"{cells[0]}: {cells[1]}")
        else:
            # 列数不是 2 的表格（比如多列对比表），用竖线连起来
            lines.append(" | ".join(c for c in cells if c))

    return "\n".join(lines)


def _load_xlsx(path: Path) -> list[Document]:
    """解析 Excel，每个工作表转成一段文字。

    分两种情况处理，因为商品资料里的 Excel 主要就是这两种形态：

    两列表（最常见）：左边参数名、右边参数值，本质是"键值对清单"
        电池容量 | 5000mAh
        机身重量 | 198g
        → 转成「电池容量: 5000mAh」这种一行一条的形式，检索效果最好

    多列表：第一行是表头，后面每行是一条记录
        商品名 | 价格 | 库存
        星耀X9 | 3999 | 120
        → 转成「商品名: 星耀X9 / 价格: 3999 / 库存: 120」

    为什么不能一律"第一行当表头"：两列的参数表第一行往往就是真实数据
    （比如「电池容量 | 5000mAh」），当成表头会导致整个表格错位，
    变成「参数名称: 电池容量」这种张冠李戴的结果。
    """
    from openpyxl import load_workbook

    # read_only=True 让大文件也能快速打开，不把整个表读进内存；
    # data_only=True 表示读公式的计算结果而不是公式本身
    workbook = load_workbook(str(path), read_only=True, data_only=True)

    docs: list[Document] = []
    try:
        for sheet in workbook.worksheets:
            rows = [
                row
                for row in sheet.iter_rows(values_only=True)
                if any(c is not None and str(c).strip() for c in row)  # 过滤空行
            ]
            if not rows:
                continue

            # 用最宽的一行来决定列数（有些表后面的行会少几列）
            ncols = max(len(row) for row in rows)

            lines: list[str] = []
            if ncols == 2:
                # 键值对表：每一行都直接转成「字段: 值」，不做表头假设
                for row in rows:
                    cells = [str(c).strip() if c is not None else "" for c in row]
                    if len(cells) < 2:
                        # 只有一列有值，就当普通文本
                        lines.append(next((c for c in cells if c), ""))
                    elif cells[0] and cells[1]:
                        lines.append(f"{cells[0]}: {cells[1]}")
                    elif cells[0] or cells[1]:
                        lines.append(cells[0] or cells[1])
            else:
                # 多列表：第一行当表头
                header = [
                    str(c).strip() if c is not None else f"第{i + 1}列"
                    for i, c in enumerate(rows[0])
                ]
                for row in rows[1:]:
                    pairs = [
                        f"{header[i] if i < len(header) else f'第{i + 1}列'}: {str(c).strip()}"
                        for i, c in enumerate(row)
                        if c is not None and str(c).strip()
                    ]
                    if pairs:
                        lines.append("\n".join(pairs))

            lines = [line for line in lines if line.strip()]
            if lines:
                docs.append(
                    Document(
                        page_content="\n\n".join(lines),
                        metadata={"sheet": sheet.title},
                    )
                )
    finally:
        workbook.close()

    if not docs:
        raise ValueError("这个 Excel 里没有提取到任何内容")
    return docs


def _load_text(path: Path) -> list[Document]:
    """解析纯文本 / Markdown。

    编码处理分两步走：
      1. 先看文件开头的 BOM（字节顺序标记），它明确告诉了我们编码是什么
      2. 没有 BOM 才去猜：先试 UTF-8，失败再试 GBK

    为什么不能简单地"依次试各种编码"：带 BOM 的 UTF-8 文件用 utf-8 解码
    也会成功，只是开头会留下一个看不见的 \\ufeff 字符。试错法根本发现不了
    这个问题，那个隐形字符会一直跟着正文进入向量库，污染检索结果。
    """
    raw = path.read_bytes()

    content: str
    if raw.startswith(b"\xef\xbb\xbf"):
        content = raw.decode("utf-8-sig")  # utf-8-sig 会自动吃掉 BOM
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        # 这两种开头是 UTF-16 的 BOM（小端 / 大端）
        content = raw.decode("utf-16")
    else:
        decoded: str | None = None
        for encoding in ("utf-8", "gbk"):
            try:
                decoded = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if decoded is None:
            raise ValueError("无法识别这个文本文件的编码，请另存为 UTF-8 后重试")
        content = decoded

    content = content.strip()
    if not content:
        raise ValueError("这个文件是空的")

    return [Document(page_content=content, metadata={})]


# 扩展名 → 解析函数
_LOADERS = {
    ".pdf": _load_pdf,
    ".docx": _load_docx,
    ".xlsx": _load_xlsx,
    ".txt": _load_text,
    ".md": _load_text,
}


# ============================================================
# 对外入口
# ============================================================
def load_document(path: str | Path, filename: str, extra_metadata: dict | None = None) -> list[Document]:
    """解析一个文件，返回带好元数据的 Document 列表。

    参数：
        path: 文件在磁盘上的实际路径
        filename: 用户看到的原始文件名（落盘时名字会加 uuid 前缀，展示要用原名）
        extra_metadata: 额外要写进每个 Document 的元数据，
                        比如 {"kb_id": 1, "doc_id": 5, "file_type": ".pdf"}

    返回的每个 Document 的 metadata 都包含：
        filename —— 原始文件名（回答时用来告诉用户"来自哪份资料"）
        file_type —— 文件类型
        page / sheet —— 定位信息（如果该格式有的话）
        以及 extra_metadata 里的所有键
    """
    path = Path(path)
    suffix = path.suffix.lower()

    loader = _LOADERS.get(suffix)
    if loader is None:
        supported = "、".join(sorted(_LOADERS))
        raise UnsupportedFileTypeError(
            f"不支持的文件类型「{suffix}」，目前支持：{supported}"
        )

    docs = loader(path)

    # 统一补充公共元数据
    common = {"filename": filename, "file_type": suffix}
    if extra_metadata:
        common.update(extra_metadata)

    for doc in docs:
        doc.metadata.update(common)

    logger.info("解析完成：%s（%d 个片段）", filename, len(docs))
    return docs


def is_supported(filename: str) -> bool:
    """判断文件类型是否支持。上传接口用它做前置校验，早点拒绝比传完再失败好。"""
    return Path(filename).suffix.lower() in _LOADERS


def supported_extensions() -> set[str]:
    """返回支持的全部扩展名，用于提示信息。"""
    return set(_LOADERS)
