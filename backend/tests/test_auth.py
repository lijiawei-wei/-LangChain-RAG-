"""认证功能测试。

覆盖：注册、登录、令牌刷新、改密、权限隔离。
运行方式（在 backend 目录下）：
    .venv/Scripts/python.exe -m pytest tests/ -v
"""

import pytest
from fastapi import Depends

from app.core.deps import require_admin
from app.main import app

API = "/api/v1"


# ============================================================
# 注册
# ============================================================
class TestRegister:
    def test_register_success(self, client):
        resp = client.post(
            f"{API}/auth/register",
            json={"username": "alice", "password": "alice123456"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["code"] == 0
        assert body["data"]["username"] == "alice"
        # 注册出来的永远是普通用户，不可能通过接口注册成管理员
        assert body["data"]["role"] == "user"

    def test_register_duplicate_username(self, client):
        payload = {"username": "bob", "password": "bob123456"}
        assert client.post(f"{API}/auth/register", json=payload).status_code == 201

        # 第二次注册同名账号应该被拒绝
        resp = client.post(f"{API}/auth/register", json=payload)
        assert resp.status_code == 409
        assert "已被注册" in resp.json()["message"]

    @pytest.mark.parametrize(
        "username,password",
        [
            ("ab", "valid123456"),  # 用户名太短
            ("has space", "valid123456"),  # 用户名含空格
            ("bad@char", "valid123456"),  # 用户名含非法字符
            ("goodname", "12345"),  # 密码太短
        ],
    )
    def test_register_invalid_input(self, client, username, password):
        resp = client.post(
            f"{API}/auth/register", json={"username": username, "password": password}
        )
        assert resp.status_code == 422

    def test_response_never_leaks_password_hash(self, client):
        """响应体里绝对不能出现密码哈希。"""
        resp = client.post(
            f"{API}/auth/register",
            json={"username": "carol", "password": "carol123456"},
        )
        assert "password" not in resp.text.lower().replace("password_hash", "")


# ============================================================
# 登录
# ============================================================
class TestLogin:
    def test_login_success(self, client, registered_user):
        username, password = registered_user
        resp = client.post(
            f"{API}/auth/login", json={"username": username, "password": password}
        )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["access_token"]
        assert data["refresh_token"]
        # 两个令牌必须不同，否则刷新机制形同虚设
        assert data["access_token"] != data["refresh_token"]
        assert data["user"]["username"] == username

    def test_login_wrong_password(self, client, registered_user):
        username, _ = registered_user
        resp = client.post(
            f"{API}/auth/login", json={"username": username, "password": "wrongpassword"}
        )
        assert resp.status_code == 401

    def test_login_nonexistent_user(self, client):
        resp = client.post(
            f"{API}/auth/login", json={"username": "nobody", "password": "whatever123"}
        )
        assert resp.status_code == 401

    def test_error_message_hides_whether_user_exists(self, client, registered_user):
        """用户不存在和密码错误，提示必须一模一样，防止枚举账号。"""
        username, _ = registered_user

        wrong_pwd = client.post(
            f"{API}/auth/login", json={"username": username, "password": "wrong123456"}
        )
        no_such = client.post(
            f"{API}/auth/login", json={"username": "ghost", "password": "wrong123456"}
        )
        assert wrong_pwd.json()["message"] == no_such.json()["message"]


# ============================================================
# 当前用户信息
# ============================================================
class TestMe:
    def test_me_without_token(self, client):
        assert client.get(f"{API}/auth/me").status_code == 401

    def test_me_with_invalid_token(self, client):
        resp = client.get(f"{API}/auth/me", headers={"Authorization": "Bearer not.a.token"})
        assert resp.status_code == 401

    def test_me_success(self, client, auth_headers, registered_user):
        username, _ = registered_user
        resp = client.get(f"{API}/auth/me", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["username"] == username


# ============================================================
# 令牌刷新
# ============================================================
class TestRefresh:
    def _login(self, client, username, password):
        return client.post(
            f"{API}/auth/login", json={"username": username, "password": password}
        ).json()["data"]

    def test_refresh_success(self, client, registered_user):
        username, password = registered_user
        tokens = self._login(client, username, password)

        resp = client.post(
            f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert resp.status_code == 200

        new_access = resp.json()["data"]["access_token"]
        # 新令牌要能正常访问接口
        me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {new_access}"})
        assert me.status_code == 200

    def test_access_token_cannot_be_used_to_refresh(self, client, registered_user):
        """拿访问令牌来换新令牌必须被拒绝，否则访问令牌的短有效期就没意义了。"""
        username, password = registered_user
        tokens = self._login(client, username, password)

        resp = client.post(
            f"{API}/auth/refresh", json={"refresh_token": tokens["access_token"]}
        )
        assert resp.status_code == 401

    def test_refresh_token_cannot_access_business_api(self, client, registered_user):
        """刷新令牌不能直接用来访问业务接口。"""
        username, password = registered_user
        tokens = self._login(client, username, password)

        resp = client.get(
            f"{API}/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"}
        )
        assert resp.status_code == 401

    def test_garbage_refresh_token(self, client):
        resp = client.post(f"{API}/auth/refresh", json={"refresh_token": "garbage"})
        assert resp.status_code == 401


# ============================================================
# 修改密码
# ============================================================
class TestChangePassword:
    def test_change_password_success(self, client, auth_headers):
        resp = client.put(
            f"{API}/auth/password",
            headers=auth_headers,
            json={"old_password": "test123456", "new_password": "newpass123456"},
        )
        assert resp.status_code == 200

        # 新密码能登录
        ok = client.post(
            f"{API}/auth/login", json={"username": "tester", "password": "newpass123456"}
        )
        assert ok.status_code == 200

    def test_wrong_old_password_rejected(self, client, auth_headers):
        resp = client.put(
            f"{API}/auth/password",
            headers=auth_headers,
            json={"old_password": "totallywrong", "new_password": "newpass123456"},
        )
        assert resp.status_code == 400

    def test_new_password_must_differ(self, client, auth_headers):
        resp = client.put(
            f"{API}/auth/password",
            headers=auth_headers,
            json={"old_password": "test123456", "new_password": "test123456"},
        )
        assert resp.status_code == 400

    def test_old_token_invalidated_after_change(self, client, auth_headers):
        """改密后，旧令牌必须立刻失效——这是 token_version 机制的核心验收点。"""
        old_token = auth_headers["Authorization"]

        resp = client.put(
            f"{API}/auth/password",
            headers=auth_headers,
            json={"old_password": "test123456", "new_password": "newpass123456"},
        )
        assert resp.status_code == 200

        # 用改密前那把旧令牌访问，应该被拒
        stale = client.get(f"{API}/auth/me", headers={"Authorization": old_token})
        assert stale.status_code == 401
        assert "密码已变更" in stale.json()["message"]

        # 改密接口返回的新令牌应当可用
        fresh_token = resp.json()["data"]["access_token"]
        good = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {fresh_token}"})
        assert good.status_code == 200

    def test_old_refresh_token_invalidated_after_change(self, client, registered_user):
        """改密后，旧的刷新令牌也不能再用来续期。"""
        username, password = registered_user
        tokens = client.post(
            f"{API}/auth/login", json={"username": username, "password": password}
        ).json()["data"]

        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        client.put(
            f"{API}/auth/password",
            headers=headers,
            json={"old_password": password, "new_password": "another123456"},
        )

        resp = client.post(
            f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert resp.status_code == 401


# ============================================================
# 管理员权限隔离
# ============================================================
class TestAdminGuard:
    """验证「普通用户不能访问管理员接口」这条硬性要求。

    这里临时挂一个测试路由来验证 require_admin 依赖本身，
    等 M2 有了真正的管理接口后，会改成直接测那些真实接口。
    """

    @pytest.fixture(autouse=True)
    def _add_test_route(self):
        @app.get("/api/v1/_test/admin-only")
        def admin_only(_=Depends(require_admin)):
            return {"code": 0, "message": "ok", "data": "管理员专属内容"}

        yield
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != "/api/v1/_test/admin-only"
        ]

    def test_anonymous_rejected(self, client):
        assert client.get(f"{API}/_test/admin-only").status_code == 401

    def test_normal_user_forbidden(self, client, auth_headers):
        """普通用户（已登录）访问管理员接口 → 403，而不是 401。

        401 表示「你没登录」，403 表示「你登录了但权限不够」，
        前端据此决定是跳登录页还是提示无权限，所以这个区分很重要。
        """
        resp = client.get(f"{API}/_test/admin-only", headers=auth_headers)
        assert resp.status_code == 403

    def test_admin_allowed(self, client, db_session):
        """管理员可以正常访问。"""
        from app.core.security import hash_password
        from app.models.user import User

        admin = User(
            username="admin_test",
            password_hash=hash_password("admin123456"),
            role="admin",
        )
        db_session.add(admin)
        db_session.commit()

        token = client.post(
            f"{API}/auth/login",
            json={"username": "admin_test", "password": "admin123456"},
        ).json()["data"]["access_token"]

        resp = client.get(
            f"{API}/_test/admin-only", headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200
        assert resp.json()["data"] == "管理员专属内容"


# ============================================================
# 密码哈希单元测试（不经过 HTTP 层）
# ============================================================
class TestPasswordHashing:
    def test_hash_and_verify(self):
        from app.core.security import hash_password, verify_password

        hashed = hash_password("mysecret123")
        assert hashed != "mysecret123"
        assert verify_password("mysecret123", hashed)
        assert not verify_password("wrongpassword", hashed)

    def test_same_password_different_hash(self):
        """同样密码两次哈希结果应该不同（bcrypt 每次加随机盐）。"""
        from app.core.security import hash_password

        assert hash_password("same123456") != hash_password("same123456")

    def test_long_chinese_password(self):
        """超长中文密码必须完整生效，不能被 bcrypt 的 72 字节上限截断。

        一个汉字 UTF-8 占 3 字节，30 个汉字就是 90 字节，超过了 bcrypt
        能处理的上限。哈希前先做 SHA-256 就是为了解决这个问题。
        """
        from app.core.security import hash_password, verify_password

        pwd_a = "这是一个非常长的中文密码用来测试截断问题" * 2 + "AAA"
        pwd_b = "这是一个非常长的中文密码用来测试截断问题" * 2 + "BBB"

        hashed = hash_password(pwd_a)
        assert verify_password(pwd_a, hashed)
        # 如果被截断了，pwd_b 会被误判为正确——这正是要防住的情况
        assert not verify_password(pwd_b, hashed)

    def test_verify_with_corrupted_hash(self):
        """数据库里的哈希被改坏时，应该返回 False 而不是抛异常。"""
        from app.core.security import verify_password

        assert not verify_password("anything", "not-a-valid-bcrypt-hash")
