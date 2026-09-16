"""加权 RRF（倒数排名融合）。

**解决什么问题**：混合检索会得到两组结果——向量检索一组、关键词检索一组。
怎么把它们合并成一个排序？

**为什么不能直接把分数加起来**：
向量相似度是 0~1 之间的小数，而 BM25 分数可能是 0~20 的任意数，
量纲完全不一样。直接相加的话，BM25 的分数会彻底压过向量分数，
等于把向量检索这条路废掉了。虽然可以先做归一化，但 BM25 分数的分布
因知识库而异，归一化后的结果并不稳定。

**RRF 的做法**：干脆不看分数，**只看排名**。

    融合得分 = Σ  权重ᵢ / (k + 排名ᵢ)

一个分块如果在向量检索里排第 1、在关键词检索里排第 3，
它的得分就是 w₁/(60+1) + w₂/(60+3)。

这样做的好处：
  1. 天然免疫量纲问题——只用到排名这个相对信息
  2. 对异常分数不敏感——某一路给出一个离谱的高分，也不会带偏整体
  3. 简单、无需训练、效果稳定，是工业界广泛采用的做法

k 取 60 是文献里的经验值。它的作用是"削峰"：k 越大，
第 1 名和第 2 名的得分差距越小，避免某一路的头部结果过度主导融合结果。
"""

import logging
from collections import defaultdict

from langchain_core.documents import Document

logger = logging.getLogger(__name__)

# RRF 公式里的平滑常数。60 是常用取值，通常不需要调整
DEFAULT_RRF_K = 60


def _doc_key(doc: Document) -> str:
    """给分块算一个唯一标识，用于在两路结果之间识别"这是同一个分块"。

    优先用 chunk_id（写向量库时生成并写进元数据的）。
    万一没有（比如老数据），退而用「文档 ID + 分块序号」拼一个——
    总比用正文内容当键好，那样内容稍改一点就被当成两个不同的分块了。
    """
    metadata = doc.metadata or {}

    if metadata.get("chunk_id"):
        return str(metadata["chunk_id"])

    return f"doc{metadata.get('doc_id', '?')}_c{metadata.get('chunk_index', '?')}"


def reciprocal_rank_fusion(
    result_lists: list[list[tuple[Document, float]]],
    weights: list[float] | None = None,
    k: int = DEFAULT_RRF_K,
    top_n: int | None = None,
) -> list[tuple[Document, float]]:
    """把多路检索结果融合成一个排序。

    参数：
        result_lists: 每路检索的结果，形如 [[(分块, 分数), ...], [(分块, 分数), ...]]
                      **列表顺序即排名顺序**（第 0 个就是该路的第一名）
        weights: 每路的权重，长度要和 result_lists 一致；不传则等权
        k: RRF 平滑常数
        top_n: 只返回前 N 个；不传则全部返回

    返回：融合后的 (分块, 融合得分) 列表，按得分从高到低排序。
          注意这里的"得分"是 RRF 分数（通常在 0.01~0.03 这个量级），
          它的绝对值没有意义，只有相对大小有意义。
    """
    if not result_lists:
        return []

    if weights is None:
        weights = [1.0] * len(result_lists)

    if len(weights) != len(result_lists):
        raise ValueError(
            f"权重数量（{len(weights)}）必须和检索路数（{len(result_lists)}）一致"
        )

    scores: dict[str, float] = defaultdict(float)
    doc_map: dict[str, Document] = {}
    # 记录每路里排第几，用于日志分析（可以看到某个分块是靠哪一路捞上来的）
    hit_sources: dict[str, list[str]] = defaultdict(list)

    for path_index, (results, weight) in enumerate(zip(result_lists, weights, strict=True)):
        if weight <= 0:
            continue

        for rank, (doc, _score) in enumerate(results):
            key = _doc_key(doc)
            doc_map[key] = doc

            # rank 从 0 开始，公式里用 1 开始更符合直觉
            scores[key] += weight / (k + rank + 1)
            hit_sources[key].append(f"路{path_index}:第{rank + 1}名")

    merged = [(doc_map[key], score) for key, score in scores.items()]
    merged.sort(key=lambda pair: pair[1], reverse=True)

    if merged:
        top_key = _doc_key(merged[0][0])
        logger.debug(
            "RRF 融合：%s 路结果共 %d 个分块，去重后 %d 个；"
            "首位分块来自 %s，融合得分 %.4f",
            len(result_lists),
            sum(len(r) for r in result_lists),
            len(merged),
            "、".join(hit_sources[top_key]),
            merged[0][1],
        )

    return merged[:top_n] if top_n else merged


def analyze_fusion(result_lists: list[list[tuple[Document, float]]]) -> dict:
    """分析多路召回的重合情况，用于调试和论文实验。

    返回各路命中了多少、两两之间重合多少。
    如果两路结果几乎完全重合，说明其中一路没起到互补作用，
    可能需要调整权重或者检查关键词检索是否正常工作。
    """
    if len(result_lists) < 2:
        return {"paths": len(result_lists), "overlap": 0}

    key_sets = [{_doc_key(doc) for doc, _ in results} for results in result_lists]

    overlaps = []
    for i in range(len(key_sets)):
        for j in range(i + 1, len(key_sets)):
            common = len(key_sets[i] & key_sets[j])
            overlaps.append(common)

    union = len(set().union(*key_sets))

    return {
        "paths": len(result_lists),
        "sizes": [len(s) for s in key_sets],
        "union": union,
        "overlaps": overlaps,
        "overlap_ratio": round(sum(overlaps) / union, 3) if union else 0,
    }
