"""引用溯源：把检索到的分块变成带编号的来源信息。

这是整个系统里「让用户信任回答」的关键一环。没有它，用户只能选择
相信或怀疑模型；有了它，用户可以点开编号亲自核对原文。

注意一个重要的设计取舍：引用信息里存的是**原文快照**，不只是分块 ID。
原因是如果只存 ID，等管理员把那份文档删掉之后，用户回看历史对话时
引用就全部落空、点开是空白。存快照后，即使原文档已删除，
历史对话里的引用依然能完整展示。
"""

import json
import logging
import re

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# 原文快照存多少字。太短看不出上下文，太长会让数据库膨胀
SNAPSHOT_TEXT_LIMIT = 500

# 匹配回答里的引用标注，如 [1]、[12]
_CITATION_PATTERN = re.compile(r"\[(\d{1,2})\]")


def build_source_label(metadata: dict) -> str:
    """根据元数据生成给人看的来源描述。

    不同格式的资料能提供的位置信息不一样：
      PDF  —— 有页码，最精确
      Excel —— 有工作表名
      Word/文本 —— 只有文件名
    所以这里按有没有对应字段来决定描述成什么样子。
    """
    filename = metadata.get("filename", "未知文件")

    if metadata.get("page"):
        return f"{filename} 第 {metadata['page']} 页"
    if metadata.get("sheet"):
        return f"{filename} · {metadata['sheet']} 工作表"

    # Markdown 按标题切过，标题信息在 heading1/2/3 里
    headings = [
        metadata[key]
        for key in ("heading1", "heading2", "heading3")
        if metadata.get(key)
    ]
    if headings:
        return f"{filename} · {' > '.join(headings)}"

    return filename


def build_citations(results: list[tuple[Document, float]]) -> list[dict]:
    """把检索结果转成带编号的引用列表。

    参数 results 是 (分块, 相关度分数) 的列表，顺序即最终排序顺序。

    返回的每一项包含回答里 [n] 标注所需的全部信息，前端拿到后
    可以直接渲染来源面板，不需要再查一次库。
    """
    citations: list[dict] = []

    for position, (doc, score) in enumerate(results, start=1):
        metadata = doc.metadata or {}
        text = (doc.page_content or "").strip()

        citations.append(
            {
                # 编号从 1 开始，和参考资料里的 [1] [2] 一一对应
                "index": position,
                "chunk_id": metadata.get("chunk_id") or "",
                "doc_id": metadata.get("doc_id"),
                "filename": metadata.get("filename", "未知文件"),
                "page": metadata.get("page"),
                "sheet": metadata.get("sheet"),
                # 给人看的来源描述，比如「产品手册.pdf 第 3 页」
                "source_label": build_source_label(metadata),
                # 相关度分数，保留 4 位小数够了
                "score": round(float(score), 4),
                # 原文快照，截断保存
                "text": text[:SNAPSHOT_TEXT_LIMIT],
                "truncated": len(text) > SNAPSHOT_TEXT_LIMIT,
            }
        )

    return citations


def parse_cited_indices(answer: str, max_index: int = 99) -> list[int]:
    """从回答里找出实际用到了哪些编号。

    用途：
      1. 前端可以只展示「真正被引用到的」资料来源，把没被用到的折叠起来
      2. 评估回答的引用质量（比如引用了根本不存在的编号 [8]，说明模型在瞎标）

    会过滤掉超出范围的编号——模型偶尔会标一个不存在的号，
    这种不该当成有效引用。
    """
    if not answer:
        return []

    indices = []
    for match in _CITATION_PATTERN.finditer(answer):
        value = int(match.group(1))
        # 过滤掉 [0] 和超出资料数量的编号
        if 1 <= value <= max_index and value not in indices:
            indices.append(value)

    return sorted(indices)


def detect_invalid_citations(answer: str, citation_count: int) -> list[int]:
    """找出回答里引用了但根本不存在的编号。

    典型情况：只有 3 条资料，模型却标了 [5]。这说明它在编造引用，
    是很危险的质量信号——统计进 qa_logs 可以监控这类问题。
    """
    if not answer:
        return []

    invalid = []
    for match in _CITATION_PATTERN.finditer(answer):
        value = int(match.group(1))
        if value > citation_count and value not in invalid:
            invalid.append(value)

    return sorted(invalid)


def to_snapshot(citations: list[dict]) -> str:
    """把引用列表序列化成存数据库的 JSON 字符串。"""
    return json.dumps(citations, ensure_ascii=False)


def from_snapshot(snapshot: str | None) -> list[dict]:
    """把数据库里的 JSON 字符串还原成引用列表。

    解析失败时返回空列表而不是抛异常：历史数据可能因为版本升级格式变了，
    不该让用户打不开一条旧对话。
    """
    if not snapshot:
        return []

    try:
        data = json.loads(snapshot)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("引用快照解析失败，已忽略：%s", e)
        return []


def mark_used_citations(answer: str, citations: list[dict]) -> list[dict]:
    """给每条引用标上「这条回答有没有真的用到它」。

    为什么需要这个标记：检索回来的资料通常比真正用上的多
    （设了 5 条，模型可能只用了第 1 条）。如果不加区分地全展示，
    用户会以为 5 条都是依据，还得自己一条条点开来核对。

    更重要的是一种情况：**模型明确回答「无法回答」时**，
    此时它一条引用都没标。如果这时候还展示 5 条来源，
    会给人"这个回答是有依据的"的错觉，而事实恰恰相反。
    有了 used 标记，前端就能如实展示「本次回答未引用知识库内容」。

    另外，如果模型回答了但忘记标注来源（偶尔会不听话），
    这里会把所有引用都标成"未被引用"——这比假装它们被用了更诚实，
    相当于如实反映"模型没有给出出处"。
    """
    if not citations:
        return []

    used_indices = set(parse_cited_indices(answer, max_index=len(citations)))

    for item in citations:
        item["used"] = item["index"] in used_indices

    return citations


def split_used_citations(answer: str, citations: list[dict]) -> tuple[list[dict], list[dict]]:
    """按「回答里有没有真的引用到」把来源分成两组，返回 (被引用的, 没被引用的)。"""
    marked = mark_used_citations(answer, [dict(c) for c in citations])
    used = [c for c in marked if c["used"]]
    unused = [c for c in marked if not c["used"]]
    return used, unused
