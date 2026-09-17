"""寒暄识别测试。

这个模块存在的意义：知识库里当然没有「你是谁」的答案，这类问题检索必然为空，
以前会走到"没有找到相关资料"的兜底分支——答辩时老师随口问一句「你是谁」，
系统回一句"没找到资料"，看着就像坏了。

**这组测试里最重要的是「不能误判」那部分**。识别漏了只是回到原来的体验，
但识别错了会把用户的商品问题吞掉、回一段自我介绍——那比原来的问题更严重。
"""

import pytest

from app.rag.prompts import build_identity_reply, build_smalltalk_reply
from app.rag.smalltalk import is_smalltalk, match_smalltalk, normalize

API = "/api/v1"


# ============================================================
# 该识别的要识别出来
# ============================================================
class TestShouldMatch:
    @pytest.mark.parametrize(
        "question",
        ["你好", "您好", "你好啊", "你好呀", "你好，在吗", "在吗", "哈喽",
         "hi", "Hi", "Hello", "早上好", "在不在"],
    )
    def test_greeting(self, question):
        assert match_smalltalk(question) == "greeting"

    @pytest.mark.parametrize(
        "question",
        ["你是谁", "你叫什么", "你叫什么名字", "你是什么", "你的名字",
         "介绍一下你自己", "自我介绍", "你是做什么的"],
    )
    def test_identity(self, question):
        assert match_smalltalk(question) == "identity"

    @pytest.mark.parametrize(
        "question",
        ["你能做什么", "你会什么", "你能干什么", "有什么功能", "怎么用", "帮助", "help"],
    )
    def test_capability(self, question):
        assert match_smalltalk(question) == "capability"

    @pytest.mark.parametrize("question", ["谢谢", "多谢", "感谢", "辛苦了", "多谢，辛苦了", "thanks"])
    def test_thanks(self, question):
        assert match_smalltalk(question) == "thanks"

    @pytest.mark.parametrize("question", ["再见", "拜拜", "回头见", "bye"])
    def test_farewell(self, question):
        assert match_smalltalk(question) == "farewell"

    @pytest.mark.parametrize("question", ["  你好  ", "你好！！", "“你好”", "你好~"])
    def test_whitespace_and_punctuation_ignored(self, question):
        """标点和空格不该影响判断。用户打字时带标点太常见了。"""
        assert match_smalltalk(question) is not None

    def test_multiple_triggers_in_one_sentence(self):
        """一句话里出现多个触发词也要认出来。"""
        assert match_smalltalk("你好，在吗") == "greeting"


# ============================================================
# 不该识别的绝不能识别（最重要的一组）
# ============================================================
class TestShouldNotMatch:
    """**这些是防误判的用例，比上面那组更重要。**

    识别漏了，用户只是回到原来的体验；识别错了，用户的商品问题会被
    一段自我介绍顶掉——那比原来的问题严重得多。
    """

    @pytest.mark.parametrize(
        "question",
        [
            "你好，这款手机多少钱",
            "你好我想问一下电池容量",
            "你好，请问冰箱保修几年",
            "谢谢，那它的保修期呢",       # 前面说谢谢，后面跟着真问题
            "在吗，帮我查一下价格",
        ],
    )
    def test_greeting_plus_real_question(self, question):
        """寒暄词后面跟着真问题——这是最容易误判的一类。

        用户很自然地会先说「你好」再提问，如果只看"包不包含你好"，
        这些正经问题就会被当成寒暄吞掉。
        """
        assert match_smalltalk(question) is None

    @pytest.mark.parametrize(
        "question",
        [
            "你是什么牌子的",             # 问的是商品，不是助手
            "这款手机是谁生产的",          # 含「是谁」
            "你是谁派来的客服，我要投诉",   # 含「你是谁」，但后面有实义内容
            "你们有什么功能更全的型号吗",   # 含「有什么功能」
        ],
    )
    def test_looks_like_smalltalk_but_asks_about_product(self, question):
        """长得像寒暄、实际在问商品的问题。"""
        assert match_smalltalk(question) is None

    @pytest.mark.parametrize(
        "question",
        ["电池容量是多少", "这个耳机多少钱", "冰箱费电吗", "路由器的穿墙效果怎么样"],
    )
    def test_normal_product_questions(self, question):
        """普通商品问题当然不能命中。"""
        assert match_smalltalk(question) is None


# ============================================================
# 边界情况
# ============================================================
class TestEdgeCases:
    @pytest.mark.parametrize("question", ["", "   ", "\n\n", None])
    def test_empty_input(self, question):
        """空输入不能崩。"""
        assert match_smalltalk(question) is None

    @pytest.mark.parametrize("question", ["！！！", "。。。", "   ，，   "])
    def test_only_punctuation(self, question):
        """只有标点，去掉之后是空串，不能报错。"""
        assert match_smalltalk(question) is None

    def test_very_long_question(self):
        """很长的文本直接判定不是寒暄，不用跑完整个匹配。

        这既是性能保护，也是正确性保护——一段几百字的问题里
        包含「你好」两个字太正常了。
        """
        long_question = "你好" + "这款手机的电池容量和充电速度以及保修政策" * 10
        assert match_smalltalk(long_question) is None

    def test_normalize_removes_punctuation_and_lowercases(self):
        assert normalize("Hello，World！") == "helloworld"
        assert normalize("  你好  ") == "你好"

    def test_is_smalltalk_helper(self):
        assert is_smalltalk("你好") is True
        assert is_smalltalk("电池多大") is False


# ============================================================
# 回复文案
# ============================================================
class TestReplies:
    def test_identity_includes_kb_summary(self):
        """自我介绍要把知识库里收录了什么列出来。

        光说"我是商品助手"没用，用户不知道能问什么；
        把收录的商品名摆出来，他一眼就知道该问什么。
        """
        reply = build_identity_reply("目前收录了 17 份商品资料，包括：星耀X9Pro手机")
        assert "商品知识库助手" in reply
        assert "星耀X9Pro手机" in reply

    def test_identity_without_kb_summary(self):
        """知识库是空的时候也要能给出合理的回复，不能留个空括号。"""
        reply = build_identity_reply("")
        assert "商品知识库助手" in reply
        assert "{}" not in reply

    @pytest.mark.parametrize(
        "category", ["greeting", "identity", "capability", "thanks", "farewell"]
    )
    def test_every_category_has_a_reply(self, category):
        """每个类别都要有对应文案，不能漏。"""
        reply = build_smalltalk_reply(category, "收录了 17 份资料")
        assert reply and len(reply) > 5

    def test_unknown_category_falls_back(self):
        """遇到没见过的类别不能崩，给个安全话术。"""
        assert build_smalltalk_reply("没见过的类别") != ""


# ============================================================
# 流水线层：寒暄要被拦住，普通问题要照常走
# ============================================================
class TestShortCircuitInPipeline:
    """这一组直接测 pipeline。

    **为什么不在接口层测**：接口测试的夹具会把整个 `pipeline.answer`
    换成假的，而寒暄判定就在 pipeline 内部——夹具一换，这段逻辑就
    整个被跳过了，测了个寂寞。

    直接测 pipeline 反而更好：跑的是真实代码，而且**一次 API 调用都不产生**
    （寒暄在检索之前就返回了，压根不会去调模型）。
    """

    @pytest.mark.asyncio
    async def test_prepare_short_circuits(self):
        """prepare 遇到寒暄要直接返回答复，不做检索。"""
        from app.rag import pipeline

        prepared = await pipeline.prepare("你是谁", None, [1])

        assert prepared.direct_reply is not None
        assert "商品知识库助手" in prepared.direct_reply
        # 没有引用是应该的——这话本来就不是从资料里来的
        assert prepared.citations == []
        # 埋了个 smalltalk 阶段标记，方便事后从日志里看出走了这条路
        assert "smalltalk" in prepared.stage_latency

    @pytest.mark.asyncio
    async def test_answer_returns_intro_without_calling_model(self):
        """非流式问答遇到寒暄要直接给自我介绍。

        这是这个功能存在的全部意义——答辩时老师随口问「你是谁」，
        系统回"没找到资料"会显得整个系统是坏的。
        """
        from app.rag import pipeline

        result = await pipeline.answer("你是谁", None, [1])

        assert "商品知识库助手" in result.answer
        assert "没有找到" not in result.answer
        assert result.citations == []
        # token 数是 0 就证明没调过模型——这一问是免费的
        assert result.prompt_tokens == 0
        assert result.completion_tokens == 0

    @pytest.mark.asyncio
    async def test_answer_stream_emits_intro(self):
        """流式路径同样要拦住，且事件格式要完整。"""
        from app.rag import pipeline

        events = []
        async for name, data in pipeline.answer_stream("你好", None, [1]):
            events.append((name, data))

        names = [n for n, _ in events]
        assert "token" in names and "finished" in names

        finished = dict(events)["finished"]
        assert finished["answer"]
        assert finished["citations"] == []
        assert finished["prompt_tokens"] == 0

    @pytest.mark.asyncio
    async def test_kb_summary_is_used_in_identity_reply(self):
        """自我介绍里要带上知识库的商品清单。

        光说"我是商品助手"没用，用户不知道能问什么；
        把收录的商品名摆出来，一眼就知道该问什么。
        """
        from app.rag import pipeline

        prepared = await pipeline.prepare(
            "你是谁", None, [1], kb_summary="目前收录了 17 份商品资料，包括：星耀X9Pro手机"
        )
        assert "星耀X9Pro手机" in prepared.direct_reply


# ============================================================
# 商品清单的提取
# ============================================================
class TestBuildKbSummary:
    def test_extracts_product_names_from_filenames(self, client, admin_headers, sample_kb, db_session):
        """从文件名里提取商品名，把「产品资料」「参数表」这类后缀去掉。"""
        from app.services.chat_service import build_kb_summary

        summary = build_kb_summary(db_session, [sample_kb["id"]])
        # 测试夹具建的知识库里没有文档，返回空串是正常的
        assert summary == ""

    def test_product_name_extraction_rules(self):
        """直接测提取规则，不用建文档（更快也更全面）。"""
        from app.services.chat_service import _product_names

        names = _product_names(
            [
                "星耀X9Pro手机产品资料.md",
                "星耀X9Pro手机参数表.xlsx",     # 同一个商品，应该被合并
                "云驰AirBook14笔记本产品资料.md",
                "云驰AirBook14参数表.docx",     # 短名字，是上面那个的前缀 → 去掉
                "声动BudsPro耳机说明书.pdf",
            ]
        )
        assert "星耀X9Pro手机" in names
        assert "云驰AirBook14笔记本" in names
        # 这两个是上面两个的短版本，必须被去掉，
        # 否则自我介绍里会出现两个看起来像不同商品、实则是同一个的名字
        assert "云驰AirBook14" not in names
        assert len(names) == 3

    def test_filename_without_known_suffix(self):
        """文件名没有已知后缀时，原样用去掉扩展名的部分，不能崩。"""
        from app.services.chat_service import _product_names

        assert _product_names(["随便起的名字.txt"]) == ["随便起的名字"]

    def test_empty_list(self):
        from app.services.chat_service import _product_names

        assert _product_names([]) == []


# ============================================================
# 接口层：确认真问题不会被寒暄分支吞掉
# ============================================================
class TestRealQuestionsReachPipeline:
    @pytest.fixture
    def buyer(self, client):
        client.post(f"{API}/auth/register", json={"username": "st_user", "password": "st123456"})
        token = client.post(
            f"{API}/auth/login", json={"username": "st_user", "password": "st123456"}
        ).json()["data"]["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_real_question_still_goes_through_pipeline(
        self, client, buyer, sample_kb, fake_pipeline
    ):
        """普通商品问题必须照常走完整流程（别把功能改坏了）。"""
        resp = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池容量是多少？", "kb_id": sample_kb["id"]},
        )
        assert resp.status_code == 200
        assert len(fake_pipeline) == 1

    def test_greeting_with_real_question_still_uses_pipeline(
        self, client, buyer, sample_kb, fake_pipeline
    ):
        """「你好，电池容量是多少」是真问题，必须走检索生成。

        **这是防误判最重要的一个验证**：如果这里 fake_pipeline 是空的，
        说明用户的商品问题被当成寒暄、用一段自我介绍顶掉了——
        那比"没找到资料"更糟，因为用户的问题被完全无视了。
        """
        resp = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "你好，电池容量是多少", "kb_id": sample_kb["id"]},
        )
        assert resp.status_code == 200
        assert len(fake_pipeline) == 1
        # 走的是真流程，所以应该带引用
        assert len(resp.json()["data"]["citations"]) > 0
