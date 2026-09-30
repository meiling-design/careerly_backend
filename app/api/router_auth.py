"""Authentication endpoints: register / login / refresh / me.

Identity model (Careerly 产品裁决 D85)
-------------------------------------
```text
register   name(必填) · phone(必填) · email(选填)
login      phone(必填)          —— 无密码、无验证码
```

The account identity is the **phone number**; there is no credential in the
Careerly contract. Bearer tokens are still issued as stateless JWTs (see
``app.core.security``) so the mobile client can move to authenticated calls
without another schema change.

All responses use the same envelope as ``router_v1``:

    {"success": bool, "data": ..., "error": {code, message} | None,
     "meta": {timestamp, version: "v1"}}
"""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from app.core.auth_store import AuthStore
from app.core.security import get_current_user, issue_tokens

router = APIRouter(prefix="/api/v1")

_API_VERSION = "v1"

# ── Envelope helpers (local copy, mirrors router_v1) ───────────────────────


def _ok(data: Any = None, status_code: int = 200, meta_extra: dict | None = None) -> JSONResponse:
    """Return a success-envelope JSON response."""
    meta: dict[str, Any] = {"timestamp": time.time(), "version": _API_VERSION}
    if meta_extra:
        meta.update(meta_extra)
    return JSONResponse(
        status_code=status_code,
        content={
            "success": True,
            "data": data,
            "error": None,
            "meta": meta,
        },
    )


def _err(code: str, message: str, status_code: int = 400) -> JSONResponse:
    """Return an error-envelope JSON response (not an exception)."""
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message},
            "meta": {"timestamp": time.time(), "version": _API_VERSION},
        },
    )


# ── Request models ─────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    """POST /auth/register body — Careerly D85 field set."""

    phone: str = Field(..., min_length=5, max_length=20, description="手机号，账号身份")
    name: str = Field(..., min_length=1, max_length=50, description="姓名 / 昵称")
    email: Optional[EmailStr] = Field(None, description="邮箱（选填）")


class LoginRequest(BaseModel):
    """POST /auth/login body — phone only, no credential (D85)."""

    phone: str = Field(..., min_length=5, max_length=20)


class RefreshRequest(BaseModel):
    """POST /auth/refresh body."""

    refresh_token: str


# ── Helpers ────────────────────────────────────────────────────────────────


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    """Return the safe subset of a user dict for responses."""
    return {
        "id": user["id"],
        "phone": user["phone"],
        "name": user["name"],
        "email": user["email"],
    }


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/auth/register", status_code=201)
async def register(req: RegisterRequest) -> JSONResponse:
    """Create a new account (identity = phone) and return a fresh token pair."""
    store = AuthStore()
    phone = req.phone.strip()

    if store.phone_exists(phone):
        return _err("PHONE_EXISTS", "该手机号已注册", 409)

    if req.email and store.email_exists(req.email):
        return _err("EMAIL_EXISTS", "该邮箱已被使用", 409)

    user_id = store.create_user(phone, req.name.strip(), req.email)
    user = store.get_by_id(user_id)
    if user is None:
        return _err("INTERNAL_ERROR", "Failed to create user", 500)

    tokens = issue_tokens(user)
    return _ok({"user": _public_user(user), **tokens}, status_code=201)


@router.post("/auth/login")
async def login(req: LoginRequest) -> JSONResponse:
    """Authenticate by phone number; return a token pair.

    ⚠️ 未决项 C1b：PRD / IA 均未定义「手机号未注册」的产品规则
    （``AuthRepository.login`` 的 Mock 刻意不判断账号是否存在）。
    此处取最小实现：未注册即 401 PHONE_NOT_REGISTERED，等待产品侧裁决。

    ⚠️ 认证边界（决策 D-NEW 方案 A / P0-6）：本端点是 **DEV 认证** ——
    只凭手机号即签发 token，**无任何凭证校验**，等价于「知道手机号即可接管账号」。
    因此当 ``AUTH_MODE != dev`` 时直接拒绝（501），而不是继续签发 token：
    正式身份验证通道见 Roadmap P5-4。
    """
    from app.core.auth_mode import is_dev_auth

    if not is_dev_auth():
        return _err(
            "AUTH_MODE_UNAVAILABLE",
            "当前部署未启用开发直登；正式身份验证通道尚未上线",
            501,
        )

    store = AuthStore()
    user = store.get_by_phone(req.phone.strip())

    if user is None:
        return _err("PHONE_NOT_REGISTERED", "该手机号尚未注册", 401)
    if not user.get("is_active", 1):
        return _err("ACCOUNT_DISABLED", "账号已停用", 403)

    tokens = issue_tokens(user)
    return _ok({"user": _public_user(user), **tokens})


@router.post("/auth/refresh")
async def refresh(req: RefreshRequest) -> JSONResponse:
    """Exchange a valid refresh token for a fresh access + refresh pair."""
    from app.core.security import decode_token

    store = AuthStore()

    try:
        payload = decode_token(req.refresh_token)
    except Exception:
        return _err("INVALID_REFRESH_TOKEN", "Refresh token is invalid or expired", 401)

    if payload.get("type") != "refresh":
        return _err("INVALID_REFRESH_TOKEN", "Provided token is not a refresh token", 401)

    subject = payload.get("sub")
    if not subject or not str(subject).isdigit():
        return _err("INVALID_REFRESH_TOKEN", "Invalid refresh token subject", 401)

    user = store.get_by_id(int(subject))
    if user is None or not user.get("is_active", 1):
        return _err("INVALID_REFRESH_TOKEN", "User no longer exists or is disabled", 401)

    tokens = issue_tokens(user)
    return _ok({
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": "bearer",
        "expires_in": tokens["expires_in"],
    })


@router.get("/auth/me")
async def me(current_user: dict = Depends(get_current_user)) -> JSONResponse:
    """Return the currently authenticated user (resolved from Bearer token)."""
    return _ok({"user": _public_user(current_user)})
