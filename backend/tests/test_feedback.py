"""答案反馈（点赞/点踩）测试。

重点验证三件事：
  1. **覆盖语义**：同一用户对同一条回答反复表态，库里始终只有一行
  2. **归属隔离**：不能评价别人的回答（返回 404，不是 403）
  3. **状态回显**：拉历史消息时带上"我评价过没有"
"""

import pytest
from sqlalchemy import select

API = "/api/v1"


@pytest.fixture
def buyer(client):
    """一个已登录的普通用户。"""
    client.post(f"{API}/auth/register", json={"username": "buyer", "password": "buyer123456"})
    token = client.post(
        f"{API}/auth/login", json={"username": "buyer", "password": "buyer123456"}
    ).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def other_user(client):
    """另一个用户，用来测越权。

    注意：不用 admin_headers，管理员也**没有**评价别人回答的权限——
    评价是"用户对系统"的反馈，管理员看的是汇总结果。
    """
    client.post(f"{API}/auth/register", json={"username": "other", "password": "other123456"})
    token = client.post(
        f"{API}/auth/login", json={"username": "other", "password": "other123456"}
    ).json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def answered(client, buyer, sample_kb, fake_pipeline):
    """提一个问题，拿到助手的回答消息 ID。

    返回 (用户提问的消息 ID, 助手回答的消息 ID)。
    """
    resp = client.post(
        f"{API}/chat/query",
        headers=buyer,
        json={"question": "电池容量是多少？", "kb_id": sample_kb["id"]},
    )
    data = resp.json()["data"]
    return data["user_message_id"], data["assistant_message_id"]


# ============================================================
# 基本功能
# ============================================================
class TestSubmitFeedback:
    def test_like(self, client, buyer, answered):
        """点赞成功，返回记录。"""
        _, assistant_id = answered
        resp = client.post(
            f"{API}/messages/{assistant_id}/feedback", headers=buyer, json={"rating": 1}
        )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["rating"] == 1
        assert data["message_id"] == assistant_id

    def test_dislike_with_comment(self, client, buyer, answered):
        """点踩时可以附上原因。"""
        _, assistant_id = answered
        resp = client.post(
            f"{API}/messages/{assistant_id}/feedback",
            headers=buyer,
            json={"rating": -1, "comment": "答的不是我问的那个商品"},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["comment"] == "答的不是我问的那个商品"

    def test_comment_is_trimmed_and_blank_becomes_none(self, client, buyer, answered):
        """评论首尾空白会被去掉；只填空格等于没填。"""
        _, assistant_id = answered
        resp = client.post(
            f"{API}/messages/{assistant_id}/feedback",
            headers=buyer,
            json={"rating": 1, "comment": "   "},
        )
        assert resp.json()["data"]["comment"] is None

    @pytest.mark.parametrize("bad_rating", [0, 2, 5, -2, 100])
    def test_invalid_rating_rejected(self, client, buyer, answered, bad_rating):
        """rating 只能是 1 或 -1。

        这道校验很重要：放任客户端传 5 或 0 进来，
        看板上的满意度会算成一个莫名其妙的数字，而且很难查是哪里写坏的。
        """
        _, assistant_id = answered
        resp = client.post(
            f"{API}/messages/{assistant_id}/feedback",
            headers=buyer,
            json={"rating": bad_rating},
        )
        assert resp.status_code == 422


class TestOverwriteSemantics:
    """同一用户对同一条回答只有一条记录。"""

    def test_repeat_like_does_not_duplicate(self, client, buyer, answered, db_session):
        """连点两次赞，数据库里仍然只有一行。"""
        from app.models.feedback import Feedback

        _, assistant_id = answered
        for _ in range(2):
            client.post(
                f"{API}/messages/{assistant_id}/feedback", headers=buyer, json={"rating": 1}
            )

        rows = list(db_session.scalars(select(Feedback)).all())
        assert len(rows) == 1
        assert rows[0].rating == 1

    def test_change_mind_updates_in_place(self, client, buyer, answered, db_session):
        """先点赞后点踩，应该改掉原来那条，而不是新增一条。"""
        from app.models.feedback import Feedback

        _, assistant_id = answered
        client.post(
            f"{API}/messages/{assistant_id}/feedback", headers=buyer, json={"rating": 1}
        )
        client.post(
            f"{API}/messages/{assistant_id}/feedback",
            headers=buyer,
            json={"rating": -1, "comment": "再想想还是不对"},
        )

        rows = list(db_session.scalars(select(Feedback)).all())
        # 新增一条的话点赞数会虚高，看板上的满意度就失去意义了
        assert len(rows) == 1
        assert rows[0].rating == -1
        assert rows[0].comment == "再想想还是不对"


class TestCancelFeedback:
    def test_cancel_removes_record(self, client, buyer, answered, db_session):
        """取消评价会把记录删掉。"""
        from app.models.feedback import Feedback

        _, assistant_id = answered
        client.post(
            f"{API}/messages/{assistant_id}/feedback", headers=buyer, json={"rating": 1}
        )

        resp = client.delete(f"{API}/messages/{assistant_id}/feedback", headers=buyer)
        assert resp.status_code == 200
        assert list(db_session.scalars(select(Feedback)).all()) == []

    def test_cancel_when_nothing_to_cancel_is_ok(self, client, buyer, answered):
        """没评价过就取消，也应该成功。

        这里**故意不返回 404**：用户想要的结果就是"没有我的评价"，
        这个状态已经达成了。报错只会让前端困惑——
        尤其是网络重试导致的重复请求，用户什么都没做错却看到红色报错。
        """
        _, assistant_id = answered
        resp = client.delete(f"{API}/messages/{assistant_id}/feedback", headers=buyer)
        assert resp.status_code == 200


# ============================================================
# 权限隔离
# ============================================================
class TestFeedbackPermissions:
    def test_anonymous_rejected(self, client, answered):
        """没登录不能评价。"""
        _, assistant_id = answered
        resp = client.post(f"{API}/messages/{assistant_id}/feedback", json={"rating": 1})
        assert resp.status_code == 401

    def test_cannot_rate_others_answer(self, client, other_user, answered):
        """不能给别人的回答点赞点踩。

        返回 404 而不是 403：403 等于承认"这条消息确实存在，只是不归你"，
        可以被用来探测系统里有多少数据。404 什么都不泄露。
        """
        _, assistant_id = answered
        resp = client.post(
            f"{API}/messages/{assistant_id}/feedback", headers=other_user, json={"rating": 1}
        )
        assert resp.status_code == 404

    def test_cannot_cancel_others_feedback(self, client, buyer, other_user, answered):
        """更不能取消别人的评价。"""
        _, assistant_id = answered
        client.post(
            f"{API}/messages/{assistant_id}/feedback", headers=buyer, json={"rating": 1}
        )
        resp = client.delete(f"{API}/messages/{assistant_id}/feedback", headers=other_user)
        assert resp.status_code == 404

    def test_cannot_rate_own_question(self, client, buyer, answered):
        """只能评价助手的回答，不能评价自己的提问。"""
        user_message_id, _ = answered
        resp = client.post(
            f"{API}/messages/{user_message_id}/feedback", headers=buyer, json={"rating": 1}
        )
        assert resp.status_code == 400

    def test_unknown_message_returns_404(self, client, buyer):
        resp = client.post(
            f"{API}/messages/999999/feedback", headers=buyer, json={"rating": 1}
        )
        assert resp.status_code == 404


# ============================================================
# 状态回显
# ============================================================
class TestFeedbackIsReturnedWithMessages:
    def test_my_feedback_included_in_history(self, client, buyer, answered):
        """拉历史消息时要带上"我评价过没有"。

        不带的话，用户刷新页面后按钮会全部变回未选中，
        他会以为自己之前的评价丢了（其实还在库里）。
        """
        _, assistant_id = answered
        conversation_id = client.get(f"{API}/conversations", headers=buyer).json()["data"][
            "items"
        ][0]["id"]

        # 还没评价时是 None
        messages = client.get(
            f"{API}/conversations/{conversation_id}/messages", headers=buyer
        ).json()["data"]
        assert messages[1]["my_feedback"] is None

        client.post(
            f"{API}/messages/{assistant_id}/feedback", headers=buyer, json={"rating": -1}
        )

        messages = client.get(
            f"{API}/conversations/{conversation_id}/messages", headers=buyer
        ).json()["data"]
        assert messages[1]["my_feedback"] == -1

    def test_others_feedback_not_visible(self, client, buyer, other_user, answered, db_session):
        """别人对这个回答的评价，我看不到。

        需要绕过接口直接往库里插一条别人的评价——
        因为接口本身不允许跨用户评价（那正是上面测过的权限）。
        这里要验证的是"读取时的过滤"，是另一回事。
        """
        from app.models.feedback import Feedback
        from app.models.user import User

        _, assistant_id = answered
        other = db_session.scalar(select(User).where(User.username == "other"))
        db_session.add(Feedback(message_id=assistant_id, user_id=other.id, rating=-1))
        db_session.commit()

        conversation_id = client.get(f"{API}/conversations", headers=buyer).json()["data"][
            "items"
        ][0]["id"]
        messages = client.get(
            f"{API}/conversations/{conversation_id}/messages", headers=buyer
        ).json()["data"]
        # 别人点过踩，跟"我"没关系，我这里还是 None
        assert messages[1]["my_feedback"] is None
