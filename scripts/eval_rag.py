"""RAG 检索效果评估。

**这个脚本是干什么的**：用一批标注好的问题，自动对比不同检索配置的效果，
产出一张可以写进论文的对比表格。

**为什么需要它**：做了这么多优化（混合检索、重排、查询改写），
到底有没有用、提升了多少，不能靠"感觉好像准了一些"来回答。
必须用同一批问题跑不同配置，把数字摆出来。

**评估指标**：

  Recall@K（召回率）
      正确答案有没有出现在前 K 条结果里。衡量"有没有找到"。
      对 RAG 来说这个指标最重要——只要正确答案在候选里，
      大模型就有机会把它用上；不在候选里，后面再怎么优化都没用。

  MRR（平均倒数排名）
      正确答案排在第几位，取倒数再平均。第 1 位得 1.0，第 2 位得 0.5，
      第 3 位得 0.33……衡量"排得够不够靠前"。
      排得越靠前，送进大模型的资料越干净，回答质量越好。

  平均延迟
      单次检索耗时。注意本脚本会清空缓存后再测，保证数字真实反映计算成本。

运行方式（在项目根目录下，需要后端已启动）：
    backend/.venv/Scripts/python.exe scripts/eval_rag.py
"""

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import settings  # noqa: E402

# ============================================================
# 测试集：问题 → 正确答案所在的资料特征
# ============================================================
# 每条包含：
#   question —— 用户会怎么问
#   expect   —— 正确答案所在的文档文件名
#   keyword  —— 这段内容里必须出现的关键词（用来确认确实命中了那一节）
#
# 为什么会用关键词而不是章节名：不同格式的资料章节结构不一样
# （PDF 有页码、Excel 有工作表名），用内容关键词判断最通用。
TEST_CASES = [
    # ---------- 精确匹配型（型号、数字）----------
    ("IMX890 是什么传感器？", "星耀X9Pro手机产品资料.md", "IMX890"),
    ("星耀 X9 Pro 支持 67W 快充吗？", "星耀X9Pro手机产品资料.md", "67W"),
    ("天玑 8300 的性能怎么样？", "星耀X9标准版手机产品资料.md", "天玑 8300"),
    ("AirBook 14 的屏幕分辨率是多少？", "云驰AirBook14笔记本产品资料.md", "2240×1400"),
    ("声动 Buds Pro 的降噪深度是多少？", "声动BudsPro耳机产品资料.md", "48dB"),
    ("智净冰箱的总容积是多少升？", "智净536L冰箱产品资料.md", "536 升"),
    ("洗烘一体机的洗涤容量是多大？", "净澈10公斤洗烘一体机产品资料.md", "10 公斤"),
    ("疾风 AX3000 支持 Wi-Fi 6 吗？", "疾风AX3000路由器产品资料.md", "Wi-Fi 6"),

    # ---------- 语义匹配型（换个说法）----------
    ("手机电池多大？", "星耀X9Pro手机产品资料.md", "5000mAh"),
    ("这款耳机充满电能用多久？", "声动BudsPro耳机产品资料.md", "32 小时"),
    ("笔记本能撑一天吗？", "云驰AirBook14笔记本产品资料.md", "11 小时"),
    ("冰箱费电吗？", "智净536L冰箱产品资料.md", "一级能效"),
    ("路由器穿墙效果怎么样？", "疾风AX3000路由器产品资料.md", "信号放大器"),
    ("洗衣机噪音大不大？", "净澈10公斤洗烘一体机产品资料.md", "洗涤程序"),

    # ---------- 售后政策型 ----------
    # 注意这些问题都点名了具体商品。不点名的话「保修几年？」在 10 份资料里
    # 都能找到答案，根本无法判分——**测试集里的问题必须有唯一正确答案**，
    # 这是评估方法的基本要求，否则指标再漂亮也没有意义。
    ("星耀 X9 Pro 保修几年？", "星耀X9Pro手机产品资料.md", "12 个月整机保修"),
    ("笔记本的保修期是多久？", "云驰AirBook14笔记本产品资料.md", "24 个月"),
    ("冰箱压缩机保修多久？", "智净536L冰箱产品资料.md", "120 个月"),
    ("耳机坏了能换新吗？", "声动BudsPro耳机产品资料.md", "免费换新"),
    ("路由器可以退货吗？", "疾风AX3000路由器产品资料.md", "无理由退换"),

    # ---------- 包装与配件型 ----------
    ("包装里都有什么？", "星耀X9Pro手机产品资料.md", "取卡针"),
    ("耳机包装里有充电线吗？", "声动BudsPro耳机产品资料.md", "充电盒"),

    # ---------- 价格型 ----------
    ("这个手机多少钱？", "星耀X9Pro手机产品资料.md", "3999"),
    ("笔记本的价格是多少？", "云驰AirBook14笔记本产品资料.md", "4299"),
    ("耳机卖多少钱？", "声动BudsPro耳机产品资料.md", "599 元"),

    # ---------- 跨文档区分型（考验能否选对产品）----------
    ("标准版和 Pro 版的电池有什么区别？", "星耀X9标准版手机产品资料.md", "4800mAh"),
    ("哪款手机支持无线充电？", "星耀X9Pro手机产品资料.md", "50W 无线快充"),
    ("AirBook 的接口都有哪些？", "云驰AirBook14笔记本产品资料.md", "HDMI"),

    # ---------- 表格类资料（考验表格解析效果）----------
    ("冰箱的官方售价是多少？", "智净536L冰箱参数表.xlsx", "4599"),
    ("广州有售后服务网点吗？", "智净536L冰箱参数表.xlsx", "天河区"),
    ("AirBook 的质保期是多久？", "云驰AirBook14参数表.docx", "12 个月"),
]


# ============================================================
# 要对比的配置
# ============================================================
# 配置的排列是有讲究的——每加一个配置，都能单独回答一个问题：
#   ① 是最基础的方案，作为对照基线
#   ② 单独看关键词检索的能力
#   ③ 对比 ①，看出「加关键词路做混合」值不值
#   ④ 对比 ①，看出「加重排」值不值（注意这个对照组不能少！
#      没有它的话，看到 ⑤ 比 ① 好，你分不清是混合检索起了作用
#      还是重排起了作用——两个变量一起变，结论就没有说服力）
#   ⑤ 对比 ④，看出「在已经重排的基础上再加混合检索」还有没有额外收益
CONFIGS = [
    {
        "name": "① 纯向量检索",
        "desc": "只用 Chroma 语义检索（对照基线）",
        "mode": "vector",
        "rerank": False,
    },
    {
        "name": "② 纯关键词检索",
        "desc": "只用 jieba 分词 + BM25",
        "mode": "keyword",
        "rerank": False,
    },
    {
        "name": "③ 混合检索",
        "desc": "向量 + BM25 双路召回，加权 RRF 融合",
        "mode": "hybrid",
        "rerank": False,
    },
    {
        "name": "④ 纯向量 + 重排",
        "desc": "只加重排，不加混合检索（用来分离变量）",
        "mode": "vector",
        "rerank": True,
    },
    {
        "name": "⑤ 混合检索 + 重排",
        "desc": "完整方案（当前默认配置）",
        "mode": "hybrid",
        "rerank": True,
    },
]

TOP_K = 5  # 评估前 5 条


def clear_all_caches() -> None:
    """清空所有缓存。

    **不清缓存的话测出来的延迟是假的**——第一次检索会把查询向量缓存起来，
    后面的配置直接命中缓存，耗时接近 0，对比完全失真。
    （这个坑实际踩过：某个配置显示平均 2ms，看着很美好，其实是缓存。）
    """
    from app.core import cache as answer_cache
    from app.rag.embeddings import get_embeddings

    answer_cache.invalidate_answers()
    get_embeddings()._cache.clear()


def is_hit(doc, keyword: str) -> bool:
    """判断一条检索结果是不是我们要找的那个答案。

    **判断依据是内容关键词，不比对文件名。**

    为什么不比对文件名（这是踩过的坑）：同一份商品资料可能同时存在
    .md 和 .pdf 两个版本（内容完全一样，只是格式不同）。如果要求文件名
    精确匹配，检索返回了 PDF 版而测试集写的是 MD 版，就会被判成"未命中"——
    可实际上内容一模一样，检索完全正确。这样算出来的指标是错的，
    而且会得出"混合检索不如纯向量"这种错误结论。

    用内容关键词判断才是对的：只要召回的那一段里真的有答案，就算命中。
    而且各个商品的参数（型号、容量、价格）本来就不一样，
    关键词本身就足以区分是哪个商品，不需要文件名帮忙。
    """
    return keyword in (doc.page_content or "")


async def evaluate_config(config: dict, kb_ids: list[int]) -> dict:
    """跑完整个测试集，返回该配置的各项指标。"""
    from app.rag import pipeline

    settings.RETRIEVAL_MODE = config["mode"]
    settings.RERANK_ENABLED = config["rerank"]

    clear_all_caches()

    hits_at_1 = 0
    hits_at_k = 0
    reciprocal_ranks: list[float] = []
    latencies: list[float] = []
    misses: list[dict] = []

    for question, expect_file, keyword in TEST_CASES:
        t0 = time.perf_counter()
        results = await pipeline.retrieve(kb_ids, question)
        latencies.append((time.perf_counter() - t0) * 1000)

        rank = None
        for position, (doc, _score) in enumerate(results[:TOP_K], start=1):
            if is_hit(doc, keyword):
                rank = position
                break

        if rank == 1:
            hits_at_1 += 1
        if rank is not None:
            hits_at_k += 1
            reciprocal_ranks.append(1.0 / rank)
        else:
            reciprocal_ranks.append(0.0)
            misses.append(
                {
                    "question": question,
                    "expect": f"{expect_file} 中含「{keyword}」的段落",
                    "got": [
                        (
                            f"{d.metadata.get('filename', '?')} "
                            f"(相关度 {s:.3f})",
                            (d.page_content or "")[:40],
                        )
                        for d, s in results[:3]
                    ],
                }
            )

    n = len(TEST_CASES)
    return {
        "name": config["name"],
        "desc": config["desc"],
        "recall_at_1": hits_at_1 / n,
        "recall_at_k": hits_at_k / n,
        "mrr": sum(reciprocal_ranks) / n,
        "avg_latency_ms": sum(latencies) / n,
        "total_latency_ms": sum(latencies),
        "misses": misses,
        "total": n,
    }


def print_report(results: list[dict]) -> None:
    """把结果打印成一张对比表。"""
    print()
    print("=" * 88)
    print("  检索效果对比")
    print("=" * 88)
    print()
    print(f"  {'配置':<20} {'Recall@1':>10} {'Recall@5':>10} {'MRR':>8} {'平均延迟':>10}")
    print("  " + "-" * 84)

    baseline = results[0]
    for r in results:
        recall1 = f"{r['recall_at_1'] * 100:.1f}%"
        recallk = f"{r['recall_at_k'] * 100:.1f}%"
        mrr = f"{r['mrr']:.3f}"
        latency = f"{r['avg_latency_ms']:.0f}ms"

        # 相对基线（纯向量）的提升
        delta = ""
        if r is not baseline:
            diff = (r["recall_at_1"] - baseline["recall_at_1"]) * 100
            if abs(diff) >= 0.1:
                delta = f"  ({diff:+.1f}pt)"

        print(f"  {r['name']:<20} {recall1:>10} {recallk:>10} {mrr:>8} {latency:>10}{delta}")

    print()
    print("  指标说明：")
    print("    Recall@1 —— 最相关的第 1 条就命中的比例（越高越好）")
    print("    Recall@5 —— 前 5 条里命中的比例（RAG 里最关键：只要召回到，模型就有机会用上）")
    print("    MRR      —— 正确答案的平均排名倒数（越接近 1 说明排得越靠前）")
    print()

    # 打印未命中的问题，方便针对性优化
    best = max(results, key=lambda r: (r["recall_at_k"], r["mrr"]))
    if best["misses"]:
        print("=" * 88)
        print(f"  最佳配置（{best['name']}）仍未命中的问题")
        print("=" * 88)
        print()
        for miss in best["misses"]:
            print(f"  问：{miss['question']}")
            print(f"      期望：{miss['expect']}")
            print("      实际返回的前 3 条：")
            for filename, preview in miss["got"]:
                print(f"        - {filename}: {preview}…")
            print()
    else:
        print(f"  最佳配置（{best['name']}）在全部 {best['total']} 个问题上都命中了前 5。")


def export_markdown(results: list[dict], path: Path) -> None:
    """把结果导出成 Markdown 表格，可以直接贴进论文。"""
    lines = [
        "# RAG 检索效果对比实验",
        "",
        f"测试集规模：{results[0]['total']} 个问题，知识库共 10 份商品资料",
        "",
        "| 配置 | Recall@1 | Recall@5 | MRR | 平均延迟 |",
        "|------|----------|----------|-----|---------|",
    ]

    for r in results:
        lines.append(
            f"| {r['name']} | {r['recall_at_1'] * 100:.1f}% | "
            f"{r['recall_at_k'] * 100:.1f}% | {r['mrr']:.3f} | "
            f"{r['avg_latency_ms']:.0f}ms |"
        )

    lines += [
        "",
        "## 配置说明",
        "",
    ]
    for r in results:
        lines.append(f"- **{r['name']}**：{r['desc']}")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  结果已导出：{path}")


async def main() -> int:
    from sqlalchemy import select

    from app.db.base import SessionLocal
    from app.models.knowledge_base import KnowledgeBase

    with SessionLocal() as db:
        all_kbs = list(db.scalars(select(KnowledgeBase)).all())

    if not all_kbs:
        print("错误：系统里还没有知识库，请先运行 gen_sample_data.py 生成资料并上传。")
        return 1

    # 可以指定只评估某一个知识库：
    #     python scripts/eval_rag.py 1
    # 不指定则用第一个（通常就是主知识库）。
    # 为什么要能指定：开发过程中可能建了好几个知识库做对照实验，
    # 全部混在一起评估的话结果没有意义——测试集是针对某个特定知识库写的。
    if len(sys.argv) > 1:
        try:
            kb_id = int(sys.argv[1])
        except ValueError:
            print(f"错误：知识库 ID 必须是数字，收到的是 {sys.argv[1]!r}")
            return 1

        if kb_id not in {kb.id for kb in all_kbs}:
            print(f"错误：不存在 ID 为 {kb_id} 的知识库")
            return 1
        kb_ids = [kb_id]
    else:
        kb_ids = [all_kbs[0].id]

    kb_name = next(kb.name for kb in all_kbs if kb.id == kb_ids[0])

    print("=" * 88)
    print("  RAG 检索效果评估")
    print("=" * 88)
    print()
    print(f"  测试集：{len(TEST_CASES)} 个问题")
    print(f"  知识库：「{kb_name}」(ID: {kb_ids[0]})")
    print(f"  对比配置：{len(CONFIGS)} 种")
    print()
    print("  开始评估（每个配置都要跑完整个测试集，请稍候）…")

    results = []
    for config in CONFIGS:
        print(f"\n  正在跑 {config['name']} …", end="", flush=True)
        t0 = time.time()
        result = await evaluate_config(config, kb_ids)
        results.append(result)
        print(
            f" 完成（{time.time() - t0:.0f}s，"
            f"Recall@5={result['recall_at_k'] * 100:.0f}%）"
        )

    print_report(results)

    export_markdown(results, ROOT / "docs" / "检索效果对比实验.md")

    # 恢复默认配置
    settings.RETRIEVAL_MODE = "hybrid"
    settings.RERANK_ENABLED = True

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
