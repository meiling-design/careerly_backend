"""JWT + password hashing utilities for user authentication.

Design notes
------------
* Passwords are hashed with bcrypt via ``passlib`` (constant-work, salted).
* Session auth is stateless: a signed JWT (HS256) ``Bearer`` token is
  issued on login/register and validated on every protected request. This
  suits the Android/mobile clients that consume this API.

Configuration (environment variables)
-------------------------------------
* ``JWT_SECRET_KEY``              Secret used to sign tokens. MUST be set
                                  in production; an insecure dev default is
                                  used otherwise (with a warning).
* ``ACCESS_TOKEN_EXPIRE_MINUTES`` Lifetime of access tokens (default 60).
* ``REFRESH_TOKEN_EXPIRE_DAYS``   Lifetime of refresh tokens (default **90**;
                                  was 7 — raised in P1-6, see the comment on
                                  the constant for the reasoning).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.core.auth_mode import is_production
from app.core.config_guard import InsecureSecretConfiguration
from app.utils.logger import logger

# ── Secrets & token configuration ─────────────────────────────────────────

_DEV_INSECURE_SECRET = "dev-insecure-change-me"
# HS256 的密钥空间就是它的熵。32 字符是下限而非目标 ——
# 用 `python -c "import secrets;print(secrets.token_urlsafe(48))"` 生成。
_MIN_SECRET_LENGTH = 32

# 异常类型定义在 config_guard（**不参与重载**的模块），原因见该模块文档：
# 定义在被 reload 的模块里会让类型身份每次重载都变，except 静默失配。

SECRET_KEY = os.getenv("JWT_SECRET_KEY", _DEV_INSECURE_SECRET)

# 与 auth_mode.assert_auth_configuration_is_safe() 同样是**导入期**硬阻断。
# 这里刻意从 warning 升级为 raise：默认密钥意味着任何人都能伪造任意用户的
# token —— 那不是「配置欠佳」，是**服务等价于没有认证**。
# 告警在生产里会被忽略，启动即失败是唯一拦得住的机制。
if is_production() and (
    SECRET_KEY == _DEV_INSECURE_SECRET or len(SECRET_KEY) < _MIN_SECRET_LENGTH
):
    raise InsecureSecretConfiguration(
        "拒绝启动：ENV=production 但 JWT_SECRET_KEY 缺失、仍为默认值、或短于 "
        f"{_MIN_SECRET_LENGTH} 字符。\n"
        "  该密钥用于签发所有用户的 access / refresh token。使用默认值等于"
        "任何人皆可伪造任意账号的凭证。\n"
        "  生产环境必须设置一个长随机值，例如：\n"
        '    python -c "import secrets;print(secrets.token_urlsafe(48))"\n'
        "  然后写入部署环境变量 JWT_SECRET_KEY。"
    )
elif SECRET_KEY == _DEV_INSECURE_SECRET:
    logger.warning(
        "JWT_SECRET_KEY not set — using insecure dev default. "
        "Set JWT_SECRET_KEY in production to a long random string."
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))

# ── P1-6 / 裁决 D-B：refresh 有效期 7 天 → 90 天 ───────────────────────────
#
# 安全持久化解决的是「**同一会话内**重启 App 不失忆」；
# 过期策略解决的是「**跨周/跨月**回来还记得我是谁」。两者缺一不可 ——
# 只做持久化、不动有效期，用户超过一周没打开照样要重新登录。
#
# Careerly 是**低频**产品：用户可能几周甚至一两个月才回来一次看「成长变化」。
# 一旦正式认证（短信验证码，P5）上线，每次重登都意味着收一次短信 ——
# 7 天会把「回来看一眼」变成一件有摩擦的事。
#
# 90 天的取舍：
#   - access 仍只有 60 分钟，泄露窗口没有变大；
#   - refresh 每次使用都会**轮换**（/auth/refresh 签发新对），
#     因此实际暴露的是「连续 90 天不活跃」才失效，而非静态凭据长存 90 天；
#   - 用户主动登出 / refresh 失效 → Flutter 侧清 Keystore，凭据即消失。
#
# ⚠️ 仍缺的一环（P5）：refresh 没有服务端吊销表，改密 / 封号无法即时生效。
#    当前可接受（无密码、无短信，谈不上「改密」），接正式认证时必须补。
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "90"))

if REFRESH_TOKEN_EXPIRE_DAYS > 365:
    logger.warning(
        "REFRESH_TOKEN_EXPIRE_DAYS=%d 超过 365 天 —— 长期凭据风险，确认这是有意为之。",
        REFRESH_TOKEN_EXPIRE_DAYS,
    )

# Token-type claim used to discriminate access vs refresh tokens.
_ACCESS_TYPE = "access"
_REFRESH_TYPE = "refresh"

# ── Password hashing (bcrypt) ──────────────────────────────────────────────

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    """Hash a plaintext password with bcrypt.

    Returns the full bcrypt hash (including salt + cost factor).
    """
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Verify a plaintext password against a stored bcrypt hash."""
    return pwd_context.verify(password, password_hash)


# ── Token creation ────────────────────────────────────────────────────────

def _create_token(
    subject: str,
    token_type: str,
    expires_delta: timedelta,
    token_version: int = 0,
) -> str:
    """Build and sign a JWT for *subject* of the given *token_type*.

    ``token_version``（发布基线 / 决策 D119）
    ----------------------------------------
    这是**吊销位**。JWT 天生无状态：一旦签发，在 ``exp`` 之前谁都收不回来。
    删除账号、改密、封号都需要「立刻失效」，而我们没有 —— 也不想维护 ——
    一张吊销表（那会变成每次请求多一次查表 + 一张只增不减的表）。

    改为在用户行上存一个整数，签发时**快照进 token**，校验时与数据库当前值
    比对。注销账号即 ``token_version += 1``，所有旧 token 在下一次请求就失效。

    默认 ``0`` 是给**存量 token** 留的：它们没有这个声明，校验侧用
    ``.get("token_version", 0)`` 读，因此不会因为本次上线被集体登出；
    而真正需要吊销时（用户删除账号）版本号一加，新旧 token 一起失效。
    """
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "token_version": token_version,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(subject: str, token_version: int = 0) -> str:
    """Create a short-lived access token (used as the Bearer credential)."""
    return _create_token(
        subject,
        _ACCESS_TYPE,
        timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        token_version,
    )


def create_refresh_token(subject: str, token_version: int = 0) -> str:
    """Create a long-lived refresh token (used to mint new access tokens)."""
    return _create_token(
        subject,
        _REFRESH_TYPE,
        timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        token_version,
    )


def decode_token(token: str) -> dict:
    """Decode and validate a JWT.

    Raises ``HTTPException(401)`` when the token is expired, malformed, or
    fails signature verification.
    """
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        ) from exc


# ── Auth dependency (Bearer) ──────────────────────────────────────────────

oauth2_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(oauth2_scheme),
) -> dict:
    """FastAPI dependency resolving the current user from a Bearer token.

    Returns the user dict (id/email/username/is_active) or raises 401/403.
    """
    if creds is None or not creds.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_token(creds.credentials)

    # Access tokens only — a refresh token must not be used as a bearer.
    if payload.get("type") != _ACCESS_TYPE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
            headers={"WWW-Authenticate": "Bearer"},
        )

    subject = payload.get("sub")
    if not subject:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Resolve the user from the DB (AuthStore). The subject is the user id.
    from app.core.auth_store import AuthStore

    try:
        user_id = int(subject)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token subject",
            headers={"WWW-Authenticate": "Bearer"},
        )

    user = AuthStore().get_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.get("is_active", 1):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is disabled",
        )

    # 已注销账号（决策 D119）。软删除后密码仍然是有效的 —— 少了这一条，
    # 用户在 App 里「删除账号」之后重新登录就能拿到新版本号的 token，
    # 吊销随即被绕过。账号一旦标记 deleted 就不该再签出任何凭据。
    if user.get("status") == "deleted":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account has been deleted",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 吊销比对（决策 D119）。用户行本来就在这里读出来了，增量成本 ≈ 0。
    # 缺失声明按 0 处理 —— 见 _create_token 的说明（存量 token 不因上线被登出）。
    if int(payload.get("token_version", 0)) != int(user.get("token_version", 0) or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has been revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


# ── Token issuance helper ─────────────────────────────────────────────────

def issue_tokens(user_dict: dict) -> dict:
    """Build a token bundle for *user_dict*.

    Returns ``{"access_token", "refresh_token", "token_type": "bearer",
    "expires_in": <seconds>}``.
    """
    subject = str(user_dict["id"])
    token_version = int(user_dict.get("token_version", 0) or 0)
    return {
        "access_token": create_access_token(subject, token_version),
        "refresh_token": create_refresh_token(subject, token_version),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }
