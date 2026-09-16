"""阈值扫描：直接看不同阈值对检索指标的影响。

跑法（后端**不用**启动）：
    cd backend
    .venv/Scripts/python.exe ../scripts/sweep_threshold.py

====================================================================
为什么需要它（这是踩过一次坑才写的）
====================================================================
标定阈值时，我先只看了**分数分布**：30 道正样本里，答案所在段落的
相似度最低是 0.514，于是把阈值定在 0.45，理由是"留了余量、不会误伤"。

结果跑完整评估发现：混合检索的 Recall@1 从 83.3% 掉到了 76.7%。

**分数分布不能代替最终指标**。原因在于混合检索不是"取分数最高的几条"，
而是先用 RRF 把两路结果的**排名**融合起来——
把向量路尾部的候选剪掉，会连带改变融合后的排序，
而这一步的影响只有跑完整指标才看得出来。

所以这个脚本只做一件事：把阈值从低到高扫一遍，
每换一个值就把完整的检索指标算一遍，直接看哪个值真正最好。

成本很低：只做检索和重排，不调用生成模型。
"""

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.core.config import settings  # noqa: E402
from eval_rag import TEST_CASES, TOP_K, clear_all_caches, is_hit  # noqa: E402

# 要扫的阈值。跨度覆盖"完全不设"到"接近误伤线"
THRESHOLDS = [0.0, 0.15, 0.25, 0.35, 0.45, 0.50]

# 要观察的配置。重点是混合检索，因为它是受阈值影响最大的那一个；
# 纯向量作为对照，能看出"影响到底来自过滤本身还是来自融合"
CONFIGS = [
    ("纯向量", "vector", False),
    ("混合检索", "hybrid", False),
    ("混合+重排", "hybrid", True),
]


async def measure(mode: str, rerank: bool) -> dict:
    """跑完一遍测试集，返回指标。"""
    from app.rag import pipeline

    settings.RETRIEVAL_MODE = mode
    settings.RERANK_ENABLED = rerank
    clear_all_caches()  # 不清缓存的话延迟和结果都会被上一次的缓存污染

    hits1 = hits5 = 0
    rr_sum = 0.0

    for question, _expect, keyword in TEST_CASES:
        results = await pipeline.retrieve([KB_ID], question)

        rank = None
        for position, (doc, _score) in enumerate(results[:TOP_K], start=1):
            if is_hit(doc, keyword):
                rank = position
                break

        if rank == 1:
            hits1 += 1
        if rank is not None:
            hits5 += 1
            rr_sum += 1 / rank

    total = len(TEST_CASES)
    return {
        "recall1": hits1 / total,
        "recall5": hits5 / total,
        "mrr": rr_sum / total,
    }


async def main() -> None:
    global KB_ID

    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.models.knowledge_base import KnowledgeBase

    with SessionLocal() as db:
        kbs = list(db.scalars(select(KnowledgeBase)).all())

    if not kbs:
        print("错误：系统里没有知识库")
        sys.exit(1)

    KB_ID = kbs[0].id
    print("=" * 70)
    print("  检索阈值扫描")
    print("=" * 70)
    print(f"  知识库：「{kbs[0].name}」(ID={KB_ID})")
    print(f"  测试集：{len(TEST_CASES)} 道题")
    print(f"  扫描阈值：{THRESHOLDS}")
    print("  （只做检索和重排，不调用生成模型）")

    # 把每个配置的结果存下来：{配置名: {阈值: 指标}}
    table: dict[str, dict[float, dict]] = {}
    original = settings.SIMILARITY_THRESHOLD
    try:
        for name, mode, rerank in CONFIGS:
            table[name] = {}
            for threshold in THRESHOLDS:
                settings.SIMILARITY_THRESHOLD = threshold
                table[name][threshold] = await measure(mode, rerank)
            print(f"  [完成] {name}")
    finally:
        settings.SIMILARITY_THRESHOLD = original

    # ---------- 输出 ----------
    for name in table:
        print()
        print("=" * 70)
        print(f"  {name}")
        print("=" * 70)
        print(f"  {'阈值':<8}{'Recall@1':>10}{'Recall@5':>11}{'MRR':>10}   {'相对最优':<10}")
        print("  " + "-" * 62)

        best = max(table[name].values(), key=lambda m: (m["recall1"], m["mrr"]))
        for threshold in THRESHOLDS:
            m = table[name][threshold]
            mark = ""
            if m is best:
                mark = "  <- 最优"
            elif m["recall1"] < best["recall1"]:
                mark = f"  (-{(best['recall1'] - m['recall1']) * 100:.1f}pt)"
            print(
                f"  {threshold:<8.2f}{m['recall1']:>9.1%}{m['recall5']:>10.1%}"
                f"{m['mrr']:>10.3f}{mark}"
            )

    print()
    print("=" * 70)
    print("  怎么看这张表")
    print("=" * 70)
    print("  Recall@1 是主指标：第一条就命中正确答案的比例。")
    print("  如果有多个阈值指标相同，选**较小的那个**——")
    print("  阈值越低，过滤掉的候选越少，对'资料写得比较含蓄'的提问越宽容。")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
