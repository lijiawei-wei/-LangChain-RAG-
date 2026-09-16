"""统计看板测试。

重点验证三个容易出错的统计口径：

  1. **时区**：库里的时间是 UTC，但按天分组必须按本地时区算。
     算错的话"今日问答"要等到早上 8 点才刷新，而早上 7 点的提问会被算到昨天。
  2. **平均值要不要算缓存命中**：缓存命中的延迟恒为 0，
     混进平均值会随着命中率一起漂移，数字看起来对、其实不反映真实耗时。
  3. **空系统不能崩**：刚装好的系统一张问答记录都没有，
     看板必须能正常打开。所有除法都要防除零。
"""

from datetime import date, datetime, time, timedelta

import pytest

API = "/api/v1"

# 和 config.STATS_TIMEZONE_OFFSET_HOURS 保持一致
LOCAL_OFFSET = timedelta(hours=8)


@pytest.fixture
def buyer(client):
    client.post(f"{API}/auth/register", json={"username": "buyer", "password": "buyer123456"})
    token = client.post(
        f"{API}/auth/login", json={"username": "buyer", "password": "buyer123456"}
    ).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _local_day_start_utc(day: date) -> datetime:
    """本地某天零点对应的 UTC 时间。"""
    return datetime.combine(day, time.min) - LOCAL_OFFSET


def _add_qa_log(db, *, created_at, cache_hit=False, latency_ms=1000, user_id=None):
    """直接往库里插一条问答日志，用于构造统计场景。"""
    from app.models.qa_log import QALog

    log = QALog(
        user_id=user_id,
        question="测试问题",
        answer="测试回答",
        prompt_tokens=100,
        completion_tokens=50,
        latency_ms=latency_ms,
        cache_hit=cache_hit,
        created_at=created_at,
    )
    db.add(log)
    db.commit()
    return log


# ============================================================
# 权限
# ============================================================
class TestStatsPermissions:
    @pytest.mark.parametrize(
        "path",
        ["/admin/stats/overview", "/admin/stats/trend", "/admin/stats/top-questions",
         "/admin/stats/recent-feedback", "/admin/stats/runtime"],
    )
    def test_anonymous_rejected(self, client, path):
        assert client.get(f"{API}{path}").status_code == 401

    @pytest.mark.parametrize(
        "path",
        ["/admin/stats/overview", "/admin/stats/trend", "/admin/stats/top-questions",
         "/admin/stats/recent-feedback", "/admin/stats/runtime"],
    )
    def test_normal_user_forbidden(self, client, buyer, path):
        """普通用户不能看全站统计。

        这里面有所有人的问答内容和 token 消耗，属于运营数据，
        普通用户看到别人问了什么是不合适的。
        """
        assert client.get(f"{API}{path}", headers=buyer).status_code == 403

    def test_normal_user_can_see_own_stats(self, client, buyer):
        """但可以看自己的统计。"""
        assert client.get(f"{API}/stats/me", headers=buyer).status_code == 200

    def test_anonymous_cannot_see_own_stats(self, client):
        assert client.get(f"{API}/stats/me").status_code == 401


# ============================================================
# 空系统
# ============================================================
class TestEmptySystem:
    def test_overview_on_empty_db(self, client, admin_headers):
        """一条数据都没有时看板也要能打开，不能抛除零错误。"""
        resp = client.get(f"{API}/admin/stats/overview", headers=admin_headers)
        assert resp.status_code == 200
        data = resp.json()["data"]

        assert data["qa"]["total"] == 0
        assert data["qa"]["cache_hit_rate"] == 0.0
        assert data["qa"]["avg_latency_ms"] == 0
        assert data["feedback"]["satisfaction"] == 0.0

    def test_trend_on_empty_db_still_has_all_days(self, client, admin_headers):
        """没人提问的日子也要出现在结果里，否则折线图会跳着画。"""
        resp = client.get(f"{API}/admin/stats/trend?days=7", headers=admin_headers)
        items = resp.json()["data"]
        assert len(items) == 7
        assert all(item["questions"] == 0 for item in items)

    def test_top_questions_on_empty_db(self, client, admin_headers):
        assert client.get(f"{API}/admin/stats/top-questions", headers=admin_headers).json()[
            "data"
        ] == []

    def test_my_stats_on_empty_db(self, client, buyer):
        resp = client.get(f"{API}/stats/me", headers=buyer)
        assert resp.status_code == 200
        assert resp.json()["data"]["qa_total"] == 0


# ============================================================
# 时区（最容易算错的地方）
# ============================================================
class TestTimezoneBucketing:
    def test_early_morning_counts_as_today(self, client, admin_headers, db_session):
        """北京时间今天凌晨 0:30 的提问，必须算作**今天**。

        这条日志在 UTC 里是"昨天 16:30"。如果按 UTC 日期分组，
        它会被算到昨天去——而用户明明就是今天问的。
        这类错误不会报错，只是数字悄悄对不上，非常难发现。
        """
        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        created_at = _local_day_start_utc(today) + timedelta(minutes=30)

        _add_qa_log(db_session, created_at=created_at)

        items = client.get(f"{API}/admin/stats/trend?days=3", headers=admin_headers).json()[
            "data"
        ]
        by_date = {item["date"]: item for item in items}

        assert by_date[today.isoformat()]["questions"] == 1
        yesterday = (today - timedelta(days=1)).isoformat()
        assert by_date[yesterday]["questions"] == 0

    def test_just_before_midnight_is_yesterday(self, client, admin_headers, db_session):
        """本地昨天 23:50 的提问算昨天，边界要卡准。"""
        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        created_at = _local_day_start_utc(today) - timedelta(minutes=10)

        _add_qa_log(db_session, created_at=created_at)

        items = client.get(f"{API}/admin/stats/trend?days=3", headers=admin_headers).json()[
            "data"
        ]
        by_date = {item["date"]: item for item in items}

        yesterday = (today - timedelta(days=1)).isoformat()
        assert by_date[yesterday]["questions"] == 1
        assert by_date[today.isoformat()]["questions"] == 0

    def test_today_count_in_overview(self, client, admin_headers, db_session):
        """总览里的"今日问答"用的是同一套时区口径。"""
        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        _add_qa_log(
            db_session, created_at=_local_day_start_utc(today) + timedelta(hours=1)
        )
        # 一条上周的，不该被算进"今天"
        _add_qa_log(
            db_session, created_at=_local_day_start_utc(today) - timedelta(days=7)
        )

        data = client.get(f"{API}/admin/stats/overview", headers=admin_headers).json()["data"]
        assert data["qa"]["total"] == 2
        assert data["qa"]["today"] == 1


# ============================================================
# 缓存命中率与平均延迟
# ============================================================
class TestCacheAndLatencyMetrics:
    def test_cache_hit_rate_reflects_reality(
        self, client, buyer, sample_kb, fake_pipeline, db_session
    ):
        """同样的首轮问题问两次，第二次应该命中缓存，命中率是 50%。

        这条测试同时是一个**回归测试**：修 bug 之前，
        写日志时代码把 cache_hit 写死成了 False，看板上的命中率永远是 0%，
        而且功能本身完全正常，只有这个数字不对——非常难发现。
        """
        for _ in range(2):
            client.post(
                f"{API}/chat/query",
                headers=buyer,
                json={"question": "电池容量是多少？", "kb_id": sample_kb["id"]},
            )

        from sqlalchemy import select

        from app.models.qa_log import QALog

        logs = list(db_session.scalars(select(QALog).order_by(QALog.id)).all())
        assert len(logs) == 2
        assert logs[0].cache_hit is False
        assert logs[1].cache_hit is True

    def test_avg_latency_excludes_cache_hits(
        self, client, admin_headers, db_session
    ):
        """平均响应延迟不能把缓存命中的 0 毫秒算进去。

        一次真实生成 2000ms + 一次缓存命中 0ms：
          排除缓存命中 → 2000ms（反映真实的生成耗时）
          不排除       → 1000ms（看着更漂亮，但毫无意义——
                          命中率越高这个数字越小，和生成快不快没关系）
        """
        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        now = _local_day_start_utc(today) + timedelta(hours=1)

        _add_qa_log(db_session, created_at=now, cache_hit=False, latency_ms=2000)
        _add_qa_log(db_session, created_at=now, cache_hit=True, latency_ms=0)

        data = client.get(f"{API}/admin/stats/overview", headers=admin_headers).json()["data"]
        assert data["qa"]["avg_latency_ms"] == 2000

    def test_hit_rate_percent(self, client, admin_headers, db_session):
        """命中率按百分比返回，保留一位小数。"""
        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        now = _local_day_start_utc(today) + timedelta(hours=1)

        for _ in range(3):
            _add_qa_log(db_session, created_at=now, cache_hit=False)
        _add_qa_log(db_session, created_at=now, cache_hit=True)

        data = client.get(f"{API}/admin/stats/overview", headers=admin_headers).json()["data"]
        assert data["qa"]["cache_hit_rate"] == 25.0


# ============================================================
# 趋势
# ============================================================
class TestTrend:
    def test_days_parameter_bounds(self, client, admin_headers):
        """days 有上限，防止有人传 100000 把库拖垮。"""
        assert client.get(f"{API}/admin/stats/trend?days=0", headers=admin_headers).status_code == 422
        assert client.get(f"{API}/admin/stats/trend?days=999", headers=admin_headers).status_code == 422

    def test_tokens_summed(self, client, admin_headers, db_session):
        """token 消耗按天累加（输入 + 输出）。"""
        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        now = _local_day_start_utc(today) + timedelta(hours=1)
        _add_qa_log(db_session, created_at=now)
        _add_qa_log(db_session, created_at=now)

        items = client.get(f"{API}/admin/stats/trend?days=2", headers=admin_headers).json()["data"]
        today_item = [i for i in items if i["date"] == today.isoformat()][0]
        # 每条 100 + 50，两条共 300
        assert today_item["tokens"] == 300
        assert today_item["questions"] == 2


# ============================================================
# 热门问题
# ============================================================
class TestTopQuestions:
    def test_trailing_punctuation_merged(self, client, admin_headers, db_session):
        """句尾标点不同的问题应该算同一个。

        「电池容量是多少」「电池容量是多少？」如果各算一次，
        排行榜上全是只出现过一次的问题，就没有意义了。
        """
        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        now = _local_day_start_utc(today) + timedelta(hours=1)

        from app.models.qa_log import QALog

        for question in ["电池容量是多少", "电池容量是多少？", "电池容量是多少。"]:
            db_session.add(
                QALog(question=question, answer="x", latency_ms=100, created_at=now)
            )
        db_session.commit()

        items = client.get(
            f"{API}/admin/stats/top-questions?limit=5", headers=admin_headers
        ).json()["data"]

        assert len(items) == 1
        assert items[0]["times"] == 3

    def test_sorted_by_frequency(self, client, admin_headers, db_session):
        """问得多的排前面。"""
        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        now = _local_day_start_utc(today) + timedelta(hours=1)

        from app.models.qa_log import QALog

        for question, times in [("少人问的", 1), ("很多人问的", 5)]:
            for _ in range(times):
                db_session.add(
                    QALog(question=question, answer="x", latency_ms=100, created_at=now)
                )
        db_session.commit()

        items = client.get(
            f"{API}/admin/stats/top-questions?limit=5", headers=admin_headers
        ).json()["data"]
        assert items[0]["question"] == "很多人问的"
        assert items[0]["times"] == 5


# ============================================================
# 反馈列表
# ============================================================
class TestRecentFeedback:
    def test_dislike_list_includes_question(self, client, buyer, admin_headers, sample_kb, fake_pipeline):
        """点踩列表要能看出"用户问了什么、系统答了什么"。

        光有"被踩了 3 条"没法排查问题，必须带上问题和回答，
        管理员才能判断是知识库缺资料还是模型答偏了。
        """
        resp = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池容量是多少？", "kb_id": sample_kb["id"]},
        )
        assistant_id = resp.json()["data"]["assistant_message_id"]

        client.post(
            f"{API}/messages/{assistant_id}/feedback",
            headers=buyer,
            json={"rating": -1, "comment": "答非所问"},
        )

        items = client.get(
            f"{API}/admin/stats/recent-feedback?rating=-1", headers=admin_headers
        ).json()["data"]

        assert len(items) == 1
        item = items[0]
        assert item["question"] == "电池容量是多少？"
        assert item["comment"] == "答非所问"
        assert item["username"] == "buyer"
        assert "资料显示" in item["answer_excerpt"]

    def test_filter_by_rating(self, client, buyer, admin_headers, sample_kb, fake_pipeline):
        """能只看点赞或只看点踩。"""
        resp = client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池容量是多少？", "kb_id": sample_kb["id"]},
        )
        assistant_id = resp.json()["data"]["assistant_message_id"]
        client.post(
            f"{API}/messages/{assistant_id}/feedback", headers=buyer, json={"rating": 1}
        )

        assert (
            len(
                client.get(
                    f"{API}/admin/stats/recent-feedback?rating=-1", headers=admin_headers
                ).json()["data"]
            )
            == 0
        )
        assert (
            len(
                client.get(
                    f"{API}/admin/stats/recent-feedback?rating=1", headers=admin_headers
                ).json()["data"]
            )
            == 1
        )

    def test_invalid_rating_filter_rejected(self, client, admin_headers):
        """rating 过滤参数只能是 1 或 -1。"""
        resp = client.get(
            f"{API}/admin/stats/recent-feedback?rating=5", headers=admin_headers
        )
        assert resp.status_code == 422


# ============================================================
# 个人统计
# ============================================================
class TestMyStats:
    def test_only_counts_own_data(self, client, buyer, auth_headers, sample_kb, fake_pipeline):
        """个人统计只算自己的，不能把别人的算进来。"""
        # buyer 问一次
        client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池容量是多少？", "kb_id": sample_kb["id"]},
        )
        # 另一个人问两次
        for _ in range(2):
            client.post(
                f"{API}/chat/query",
                headers=auth_headers,
                json={"question": f"保修期多久{_}", "kb_id": sample_kb["id"]},
            )

        assert client.get(f"{API}/stats/me", headers=buyer).json()["data"]["qa_total"] == 1
        assert client.get(f"{API}/stats/me", headers=auth_headers).json()["data"]["qa_total"] == 2

    def test_counts_conversations_and_tokens(self, client, buyer, sample_kb, fake_pipeline):
        client.post(
            f"{API}/chat/query",
            headers=buyer,
            json={"question": "电池容量是多少？", "kb_id": sample_kb["id"]},
        )
        data = client.get(f"{API}/stats/me", headers=buyer).json()["data"]

        assert data["conversation_count"] == 1
        # 提问 1 条 + 回答 1 条
        assert data["message_count"] == 2
        assert data["prompt_tokens"] == 800
        assert data["completion_tokens"] == 50
        assert data["total_tokens"] == 850


# ============================================================
# 首字延迟
# ============================================================
class TestFirstTokenLatency:
    def test_average_first_token_from_stage_latency(self, client, admin_headers, db_session):
        """平均首字延迟从 stage_latency 这个 JSON 里解析出来。

        它是流式输出最该被测量的指标——用户在意的是"多久有反应"，
        而不是"总共花了多久"。光看平均延迟这个优化完全体现不出来。
        """
        import json

        from app.models.qa_log import QALog

        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        now = _local_day_start_utc(today) + timedelta(hours=1)

        for first_token in [200, 400]:
            db_session.add(
                QALog(
                    question="q",
                    answer="a",
                    latency_ms=3000,
                    stage_latency=json.dumps({"retrieve": 300, "first_token": first_token}),
                    created_at=now,
                )
            )
        db_session.commit()

        data = client.get(f"{API}/admin/stats/overview", headers=admin_headers).json()["data"]
        assert data["qa"]["avg_first_token_ms"] == 300

    def test_missing_or_broken_json_is_skipped(self, client, admin_headers, db_session):
        """老数据里没有 first_token 字段、或者 JSON 坏了，都不能让看板打不开。"""
        from app.models.qa_log import QALog

        today = (datetime.utcnow() + LOCAL_OFFSET).date()
        now = _local_day_start_utc(today) + timedelta(hours=1)

        db_session.add_all(
            [
                QALog(question="q1", answer="a", stage_latency=None, created_at=now),
                # 没有 first_token 键
                QALog(question="q2", answer="a", stage_latency='{"retrieve": 300}', created_at=now),
                # 坏掉的 JSON
                QALog(question="q3", answer="a", stage_latency="{不是合法的JSON", created_at=now),
            ]
        )
        db_session.commit()

        resp = client.get(f"{API}/admin/stats/overview", headers=admin_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["qa"]["avg_first_token_ms"] == 0


# ============================================================
# 运行时状态
# ============================================================
class TestRuntimeStats:
    def test_runtime_shape(self, client, admin_headers):
        data = client.get(f"{API}/admin/stats/runtime", headers=admin_headers).json()["data"]
        assert set(data) == {
            "cache_entries",
            "cache_maxsize",
            "cache_evictions",
            "active_concurrency",
        }
