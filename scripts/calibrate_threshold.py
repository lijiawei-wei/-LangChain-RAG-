"""检索阈值标定。

跑法（后端**不用**启动）：
    cd backend
    .venv/Scripts/python.exe ../scripts/calibrate_threshold.py

====================================================================
为什么需要这个脚本
====================================================================
项目里有两个检索过滤阈值，默认值都是拍脑袋定的，实际上形同虚设：

    SIMILARITY_THRESHOLD = 0.15   —— 向量的余弦相似度下限
    RERANK_SCORE_THRESHOLD = 0.0  —— 重排分数的下限（等于不过滤）

后果：用户问"今天天气怎么样"这种完全无关的问题，系统照样会捞回 20 条
商品资料，然后一股脑塞给大模型。虽然提示词里交代了"资料不足要拒答"，
但这既浪费 token（每次都要把无关资料送进去生成），也增加了模型被带偏的机会。

把阈值调准之后：无关问题在**检索阶段**就被拦下，
直接返回"没有找到相关资料"，连生成模型都不用调——**既准又省钱**。

====================================================================
成本控制（这个脚本几乎不花钱）
====================================================================
  - **只做向量化，不做生成**：全程不调用对话模型，而对话模型才是花钱的大头
  - 每道题只需要一次向量化（一句话十几个 token，单价极低）
  - 不经过 HTTP 接口，所以不会往 qa_logs 里写测试数据污染看板

====================================================================
方法
====================================================================
要定一个阈值，得知道两类分数的分布：

  正样本（应该通过）：30 道有标准答案的题，它们在检索结果里的相似度是多少
  负样本（应该拦掉）：15 道知识库里根本没有答案的题，它们最高拿到多少分

理想情况下这两堆分数是分开的，阈值取在中间。
如果它们重叠（也就是"无关问题"的分数比"相关问题"还高），
那就说明这个指标本身不具备区分能力——这同样是重要结论，如实报告即可。
"""

import io
import statistics
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.core.config import settings  # noqa: E402

# ============================================================
# 正样本：知识库里确实有答案的问题（和 eval_rag.py 用同一套）
# ============================================================
# 只取 (问题, 关键词)，关键词用来定位"哪一条才是正确答案所在的段落"
POSITIVE_CASES = [
    ("IMX890 是什么传感器？", "IMX890"),
    ("星耀 X9 Pro 支持 67W 快充吗？", "67W"),
    ("天玑 8300 的性能怎么样？", "天玑 8300"),
    ("AirBook 14 的屏幕分辨率是多少？", "2240×1400"),
    ("声动 Buds Pro 的降噪深度是多少？", "48dB"),
    ("智净冰箱的总容积是多少升？", "536 升"),
    ("洗烘一体机的洗涤容量是多大？", "10 公斤"),
    ("疾风 AX3000 支持 Wi-Fi 6 吗？", "Wi-Fi 6"),
    ("手机电池多大？", "5000mAh"),
    ("这款耳机充满电能用多久？", "32 小时"),
    ("笔记本能撑一天吗？", "11 小时"),
    ("冰箱费电吗？", "一级能效"),
    ("路由器穿墙效果怎么样？", "信号放大器"),
    ("洗衣机噪音大不大？", "洗涤程序"),
    ("星耀 X9 Pro 保修几年？", "12 个月整机保修"),
    ("笔记本的保修期是多久？", "24 个月"),
    ("冰箱压缩机保修多久？", "120 个月"),
    ("耳机坏了能换新吗？", "免费换新"),
    ("路由器可以退货吗？", "无理由退换"),
    ("包装里都有什么？", "取卡针"),
    ("耳机包装里有充电线吗？", "充电盒"),
    ("这个手机多少钱？", "3999"),
    ("笔记本的价格是多少？", "4299"),
    ("耳机卖多少钱？", "599 元"),
    ("标准版和 Pro 版的电池有什么区别？", "4800mAh"),
    ("哪款手机支持无线充电？", "50W 无线快充"),
    ("AirBook 的接口都有哪些？", "HDMI"),
    ("冰箱的官方售价是多少？", "4599"),
    ("广州有售后服务网点吗？", "天河区"),
    ("AirBook 的质保期是多久？", "12 个月"),
]

# ============================================================
# 负样本：知识库里**没有**答案的问题
# ============================================================
# 分两类，难度完全不同：
#
#   A 类 完全无关 —— 一眼就知道答案不在这。拦下它们很容易，
#        但这类问题在实际使用中本来就不多（用户不会对着商品库问天气）。
#
#   B 类 同领域但资料没写 —— **这才是真正难的部分**。
#        它们和商品资料语义高度接近（都在讲手机、冰箱、耳机），
#        向量检索会给很高的相似度，但资料里确实没有答案。
#        阈值能不能立住，全看它能不能拦住这一类。
NEGATIVE_CASES = [
    # ---------- A 类：完全无关 ----------
    "今天天气怎么样？",
    "推荐一部好看的电影",
    "帮我写一首关于春天的诗",
    "北京到上海的高铁要坐多久？",
    "红烧肉怎么做才好吃？",
    "帮我算一下 357 乘以 28 等于多少",
    "明天股市会涨吗？",

    # ---------- B 类：同领域，但资料里没写 ----------
    "这款手机的屏幕供应商是谁？",
    "冰箱的压缩机是什么牌子的？",
    "耳机的振膜用的是什么材质？",
    "笔记本的主板是什么型号？",
    "洗衣机的电机是哪个厂家生产的？",
    "路由器的芯片方案是哪一家提供的？",
    "这款手机在国外的售价是多少？",
    "冰箱用的制冷剂是什么型号？",
    "这批商品的库存还剩多少件？",
]


def pick_kb(db) -> tuple[int, str]:
    """挑出装有测试资料的那个知识库。"""
    from sqlalchemy import select

    from app.models.knowledge_base import KnowledgeBase

    kbs = list(db.scalars(select(KnowledgeBase)).all())
    if not kbs:
        print("错误：系统里没有任何知识库，请先上传资料")
        sys.exit(1)

    # 挑文档数最多的那个，通常就是装了示例资料的主库
    best = max(kbs, key=lambda k: k.doc_count)
    return best.id, best.name


def _rerank_top_score(question: str, results: list) -> float | None:
    """对已经召回的结果做一次重排，返回第 1 名的重排分数。

    重排失败（网络抖动、接口限流）不该让整个标定跑不下去，
    所以这里把所有异常都吞掉返回 None——相似度的标定不依赖它。
    """
    if not settings.RERANK_ENABLED or not results:
        return None

    from app.rag.reranker import rerank as rerank_documents

    try:
        reranked = rerank_documents(
            question, [doc for doc, _ in results], settings.RERANK_TOP_N
        )
    except Exception as e:  # noqa: BLE001 - 标定脚本，重排失败不影响主流程
        print(f"      （重排失败，跳过：{e}）")
        return None

    if not reranked:
        return None
    return float(reranked[0].metadata.get("rerank_score", 0.0))


def describe(name: str, values: list[float]) -> None:
    """打印一组分数的分布情况。"""
    if not values:
        print(f"  {name}: 无数据")
        return
    values = sorted(values)
    print(
        f"  {name}: {len(values)} 个   最小 {values[0]:.4f}   "
        f"中位 {statistics.median(values):.4f}   最大 {values[-1]:.4f}"
    )


def suggest_threshold(positives: list[float], negatives: list[float]) -> None:
    """扫描所有可能的阈值，找出最优的那个。

    评价标准用**平衡准确率** = (正样本通过率 + 负样本拦掉率) / 2。

    为什么不用"准确率"：两类样本的数量不相等（30 : 16），
    只看准确率的话，把所有样本都判成"通过"也能拿 65%，
    但这个阈值等于没设。平衡准确率对两类一视同仁，不会被样本数量带偏。
    """
    print()
    print("=" * 64)
    print("  标定结果")
    print("=" * 64)

    if not positives or not negatives:
        print("  样本不足，无法给出建议")
        return

    pos_min, neg_max = min(positives), max(negatives)

    print(f"  正样本（该通过）最低分：{pos_min:.4f}")
    print(f"  负样本（该拦掉）最高分：{neg_max:.4f}")

    if pos_min > neg_max:
        threshold = (pos_min + neg_max) / 2
        print(f"\n  [完全可分] 两组分数没有重叠，阈值取中间 {threshold:.4f} 即可：")
        print("     负样本全拦下，正样本一条不误伤。")
        return

    print("  [存在重叠] 分数有交叉，没有完美阈值，得做取舍。\n")

    # 在"负样本最高分"到"正样本最低分"之间细扫一遍。
    # 这个区间之外不可能更优：卡得比负样本最高分还高，正样本会开始被误伤；
    # 卡得比正样本最低分还低，负样本就一条都拦不住了
    candidates = []
    steps = 200
    lo, hi = neg_max, pos_min
    for i in range(steps + 1):
        t = lo + (hi - lo) * i / steps
        kept = sum(1 for v in positives if v >= t)
        blocked = sum(1 for v in negatives if v < t)
        tpr = kept / len(positives)
        tnr = blocked / len(negatives)
        candidates.append((t, kept, blocked, (tpr + tnr) / 2))

    best = max(candidates, key=lambda c: c[3])
    print("  按平衡准确率扫描出的最优阈值：")
    print(
        f"    阈值 {best[0]:.4f}   "
        f"正样本通过 {best[1]}/{len(positives)}，"
        f"负样本拦掉 {best[2]}/{len(negatives)}   "
        f"平衡准确率 {best[3]:.1%}"
    )

    # 再给出几个"以保正样本为先"的候选。
    # 实际用哪个取决于更在意什么：
    #   拦住无关问题 → 省 token、少干扰，但可能误伤正常提问
    #   保住正样本   → 保证该答的都能答上
    # 对问答系统来说，**误伤（该答的没答）比漏拦（多送几条无关资料）严重得多**，
    # 所以在平衡准确率之外，另外把"零误伤"这一档也列出来
    print()
    print("  如果要求**一条正样本都不能误伤**，则只能卡在正样本最低分上：")
    kept = sum(1 for v in positives if v >= pos_min)
    blocked = sum(1 for v in negatives if v < pos_min)
    print(
        f"    阈值 {pos_min:.4f}   "
        f"正样本通过 {kept}/{len(positives)}，"
        f"负样本拦掉 {blocked}/{len(negatives)}"
    )
    print(
        "    （正样本最低分是一个点估计，只由一道题决定，"
        "建议再往下留一点余量，别卡得那么死）"
    )


async def main() -> None:
    from app.db.base import SessionLocal
    from app.rag import pipeline, vectorstore

    print("=" * 64)
    print("  检索阈值标定（只做向量化，不调用生成模型）")
    print("=" * 64)

    with SessionLocal() as db:
        kb_id, kb_name = pick_kb(db)

    kb_ids = [kb_id]
    print(f"\n使用知识库：{kb_name} (id={kb_id})")
    print(f"正样本 {len(POSITIVE_CASES)} 道，负样本 {len(NEGATIVE_CASES)} 道")
    print()

    # ---------- 正样本 ----------
    print("[1/2] 跑正样本（知识库里有答案的问题）...")
    positive_scores: list[float] = []
    positive_top1: list[float] = []
    positive_rerank: list[float] = []
    misses = []

    for question, keyword in POSITIVE_CASES:
        # 只走向量检索这一步，拿到带相似度的原始结果。
        # 用 RETRIEVE_TOP_K 条，和实际检索时一致
        results = await vectorstore.search_multi(
            kb_ids, question, settings.RETRIEVE_TOP_K
        )
        if not results:
            misses.append(question)
            continue

        positive_top1.append(results[0][1])

        # 找出"答案真正所在的那一段"拿到的最高分
        hit_scores = [score for doc, score in results if keyword in (doc.page_content or "")]
        if hit_scores:
            positive_scores.append(max(hit_scores))
        else:
            misses.append(question)

        # 顺便记一下重排分数。重排是对**同一批候选**重新打分，
        # 复用的是上面已经拿到的结果，不用再检索一次
        score = _rerank_top_score(question, results)
        if score is not None:
            positive_rerank.append(score)

    print(f"      命中 {len(positive_scores)} 道，未召回答案 {len(misses)} 道")
    if misses:
        print("      未召回的题（这些是检索本身的问题，和阈值无关）：")
        for q in misses:
            print(f"        - {q}")

    # ---------- 负样本 ----------
    print()
    print("[2/2] 跑负样本（知识库里没有答案的问题）...")
    negative_scores: list[float] = []
    negative_rerank: list[float] = []

    for question in NEGATIVE_CASES:
        results = await vectorstore.search_multi(
            kb_ids, question, settings.RETRIEVE_TOP_K
        )
        if not results:
            # 一条都没召回，说明连向量库都觉得不相关，是最理想的情况
            negative_scores.append(0.0)
            continue

        negative_scores.append(results[0][1])

        score = _rerank_top_score(question, results)
        if score is not None:
            negative_rerank.append(score)

    # ---------- 报告 ----------
    print()
    print("=" * 64)
    print("  分数分布")
    print("=" * 64)
    print("\n【向量相似度】SIMILARITY_THRESHOLD")
    describe("正样本(答案所在段落)", positive_scores)
    describe("正样本(返回的第 1 条)", positive_top1)
    describe("负样本(返回的第 1 条)", negative_scores)
    suggest_threshold(positive_scores, negative_scores)

    if positive_rerank and negative_rerank:
        print()
        print("【重排分数】RERANK_SCORE_THRESHOLD")
        describe("正样本(第 1 条)", positive_rerank)
        describe("负样本(第 1 条)", negative_rerank)
        suggest_threshold(positive_rerank, negative_rerank)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
