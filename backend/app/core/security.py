"""密码哈希与 JWT 令牌。

两件事：
1. 密码用 bcrypt 加盐哈希后存库，永远不存明文；
2. 登录成功后签发 JWT，前端带着它访问其它接口。
"""

import base64
import hashlib
from datetime import timedelta
from typing import Any, Literal

import bcrypt
import jwt

from app.core.config import settings
from app.utils.time import utcnow

TokenType = Literal["access", "refresh"]


# ============================================================
# 密码哈希
# ============================================================
def _prepare(password: str) -> bytes:
    """把任意长度的密码压成固定长度字节串，再交给 bcrypt。

    为什么需要这一步：bcrypt 算法本身只处理前 72 字节，超出的部分会被
    静默丢弃。而一个汉字在 UTF-8 里占 3 字节，也就是说 24 个汉字的密码
    就会被截断——用户以为设了 30 位的强密码，实际只生效了前 24 个字。
    更糟的是 bcrypt 5.x 遇到超长密码会直接抛异常。

    这里先做一次 SHA-256 再 base64 编码，结果是固定的 44 字节，
    既避免了截断和报错，又不损失任何熵。
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """生成密码哈希，用于注册和修改密码时入库。"""
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验明文密码是否与库里的哈希匹配。

    任何异常都当作「不匹配」返回 False，不要往外抛——
    否则攻击者能通过报错类型反推出密码格式信息。
    """
    try:
        return bcrypt.checkpw(_prepare(plain_password), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


# ============================================================
# JWT 令牌
# ============================================================
def create_token(
    subject: int | str,
    token_type: TokenType,
    token_version: int = 0,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """签发 JWT。

    参数：
        subject: 用户 ID，写进标准的 sub 字段
        token_type: access（访问令牌，短期）或 refresh（刷新令牌，长期）
        token_version: 用户当前的令牌版本号。用户改密码后这个号会 +1，
            于是所有旧令牌校验时版本对不上，自动失效。
        extra_claims: 需要额外塞进令牌的信息，比如角色 role

    为什么要区分两种令牌：access 有效期短，即使泄露影响也有限；
    refresh 有效期长但只能用来换新的 access，不能直接访问业务接口。
    """
    if token_type == "access":
        expires_delta = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    else:
        expires_delta = timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    now = utcnow()
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "ver": token_version,
        "iat": now,
        "exp": now + expires_delta,
    }
    if extra_claims:
        payload.update(extra_claims)

    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    """解析并校验 JWT。

    校验包括：签名是否正确、是否已过期。
    任何一步失败都返回 None，由调用方统一处理成 401。
    """
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.PyJWTError:
        # 涵盖签名错误、过期、格式非法等各种情况
        return None
