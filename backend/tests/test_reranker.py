"""重排组件的过滤与降级逻辑测试。

这里**不测"重排准不准"**——那取决于模型，用 scripts/eval_rag.py 跑真实问题集评估。
这里测的是"拿到分数之后我们自己的代码怎么处理"，这段逻辑才是会出 bug 的地方：

    - 分数够的留下、不够的丢掉
    - 一个都没留下的时候怎么办 ← 实测踩过坑，见 TestAllBelowThreshold

接口调用被换成了假的，所以这组测试不花钱、不用联网。
"""

import pytest
from langchain_core.documents import Document

from app.rag.reranker import DashScopeReranker, RerankAPIError


def make_docs(n: int) -> list[Document]:
    return [Document(page_content=f"第 {i} 条资料", metadata={"chunk_id": f"c{i}"}) for i in range(n)]


def fake_api(scores: list[float]):
    """造一个假的 _call_api：按给定的分数返回结果，顺序打乱一点更接近真实情况。

    真实接口返回的是**按分数排好序**的结果，每条带着它在入参里的下标。
    """

    def _fake(query: str, documents: list[str], top_n: int):
        pairs = [{"index": i, "relevance_score": s} for i, s in enumerate(scores[: len(documents)])]
        pairs.sort(key=lambda item: item["relevance_score"], reverse=True)
        return pairs[:top_n]

    return _fake


@pytest.fixture
def reranker(monkeypatch):
    """造一个重排器，并把它的接口调用换成假的。"""

    def _build(scores: list[float], threshold: float = 0.10):
        instance = DashScopeReranker(api_key="fake-key", score_threshold=threshold, top_n=5)
        monkeypatch.setattr(instance, "_call_api", fake_api(scores))
        return instance

    return _build


class TestThresholdFiltering:
    def test_low_scores_are_dropped(self, reranker):
        """低于阈值的丢掉，够分的按分数从高到低留下。"""
        docs = make_docs(3)
        result = reranker([0.5, 0.02, 0.3]).rerank("问题", docs)

        assert [d.metadata["chunk_id"] for d in result] == ["c0", "c2"]
        # 分数和名次要写回 metadata，后面生成引用列表要用
        assert result[0].metadata["rerank_score"] == 0.5
        assert result[0].metadata["rerank_rank"] == 1

    def test_empty_input(self, reranker):
        """一条候选都没有时直接返回空，不该去调接口。"""
        assert reranker([]).rerank("问题", []) == []


class TestAllBelowThreshold:
    """全部低于阈值时的两种处理，**它们的含义完全不同**。

    这段逻辑是修一个真实问题改出来的：

        在「手机数码」知识库里问服装尺码（资料其实在「服装」库里），
        5 条候选的重排分最高只有 0.0060。旧逻辑会把它们照原样交给大模型，
        于是模型回一句"根据现有资料无法回答这个问题"——用户等了 7 秒，
        而且完全看不出问题出在哪（真实原因是他选错了知识库）。

    分界线取"阈值的一半"，依据是 scripts/calibrate_threshold.py 的实测分布：
    有答案的问题最高分最低也有 0.1301，而"完全不沾边"的只有 0.00x。
    """

    def test_no_signal_means_nothing_relevant(self, reranker):
        """最高分连阈值一半都够不到 = 库里压根没有相关内容，按没检索到处理。

        分数取的就是实测值：在手机知识库里问服装尺码，5 条候选的最高分是 0.0060。
        返回空之后，流水线会走"没找到资料"那条固定答复：瞬时返回、
        文案里还会提醒用户检查检索范围——比让模型硬答一句"无法回答"清楚得多。
        """
        docs = make_docs(5)
        assert reranker([0.0060, 0.0041, 0.0022, 0.0013, 0.0009]).rerank("问题", docs) == []

    def test_exactly_zero_also_means_no_signal(self, reranker):
        docs = make_docs(3)
        assert reranker([0.0, 0.0, 0.0]).rerank("问题", docs) == []

    def test_low_but_near_threshold_still_falls_back(self, reranker):
        """分数偏低但**够得着阈值一半**：资料可能是有的，仍然降级返回。

        这是要保住的原有行为——真正的相关资料不该因为阈值调高一点就整个消失。
        正样本实测最低 0.1301，和这条线（0.05）之间还有 2.6 倍余量。
        """
        docs = make_docs(3)
        result = reranker([0.07, 0.05, 0.03], threshold=0.10).rerank("问题", docs)

        assert len(result) == 3, "分数偏低但仍有可能相关时，应该降级返回原始召回结果"

    def test_boundary_is_half_of_threshold(self, reranker):
        """刚好卡在阈值一半上：算有信号，走降级（边界取闭区间）。"""
        docs = make_docs(2)
        assert len(reranker([0.05, 0.01], threshold=0.10).rerank("问题", docs)) == 2

    def test_threshold_disabled_keeps_everything(self, reranker):
        """阈值设成 0 等于关闭过滤：0 分也算通过，全部候选原样返回。"""
        docs = make_docs(3)
        result = reranker([0.0, 0.0, 0.01], threshold=0.0).rerank("问题", docs)

        assert len(result) == 3


class TestAPIFailure:
    def test_falls_back_to_original_order(self, reranker, monkeypatch):
        """重排接口挂了要降级用召回顺序，不能让整个问答不可用。

        重排只是精度优化：它挂了顶多答得没那么准，但不该答不了。
        """
        docs = make_docs(3)
        instance = reranker([0.5])

        def _boom(query, documents, top_n):
            raise RerankAPIError("接口超时")

        monkeypatch.setattr(instance, "_call_api", _boom)

        result = instance.rerank("问题", docs)
        assert [d.metadata["chunk_id"] for d in result] == ["c0", "c1", "c2"]

    def test_out_of_range_index_is_skipped(self, reranker, monkeypatch):
        """接口返回越界下标时跳过那一条，不能让整个检索崩掉。"""
        docs = make_docs(2)
        instance = reranker([0.5, 0.4])

        def _weird(query, documents, top_n):
            return [
                {"index": 0, "relevance_score": 0.5},
                {"index": 99, "relevance_score": 0.4},  # 越界
                {"index": 1, "relevance_score": 0.3},
            ]

        monkeypatch.setattr(instance, "_call_api", _weird)

        result = instance.rerank("问题", docs)
        assert [d.metadata["chunk_id"] for d in result] == ["c0", "c1"]
