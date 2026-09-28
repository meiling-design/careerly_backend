"""Authentication endpoints: register / login / refresh / me.

All responses use the same envelope as ``router_v1``:

    {"success": bool, "data": ..., "error": {code, message} | None,
     "meta": {timestamp, version: "v1"}}

Bearer tokens are issued as stateless JWTs (see ``app.core.security``).
This module keeps a *local* copy of the envelope helpers to avoid a
cross-module dependency on ``router_v1``.
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
    """POST /auth/register body."""

    email: EmailStr
    username: Optional[str] = Field(None, description="Optional display/username")
    password: str = Field(..., min_length=8, description="At least 8 characters")
    confirm_password: str


class LoginRequest(BaseModel):
    """POST /auth/login body. identifier may be email or username."""

    identifier: str = Field(..., description="Email or username")
    password: str


class RefreshRequest(BaseModel):
    """POST /auth/refresh body."""

    refresh_token: str


# ── Helpers ────────────────────────────────────────────────────────────────


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    """Return the safe subset of a user dict for responses (no internal fields)."""
    return {
        "id": user["id"],
        "email": user["email"],
        "username": user["username"],
    }


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/auth/register", status_code=201)
async def register(req: RegisterRequest) -> JSONResponse:
    """Create a new account and return the user + a fresh token pair."""
    from app.core.security import hash_password

    store = AuthStore()

    if req.password != req.confirm_password:
        return _err("PASSWORD_MISMATCH", "Passwords do not match", 400)

    if store.email_exists(req.email):
        return _err("EMAIL_EXISTS", "Email already registered", 409)

    if req.username and store.username_exists(req.username):
        return _err("USERNAME_EXISTS", "Username already taken", 409)

    password_hash = hash_password(req.password)
    user_id = store.create_user(req.email, req.username, password_hash)
    user = store.get_by_id(user_id)
    if user is None:
        return _err("INTERNAL_ERROR", "Failed to create user", 500)

    tokens = issue_tokens(user)
    return _ok({"user": _public_user(user), **tokens}, status_code=201)


@router.post("/auth/login")
async def login(req: LoginRequest) -> JSONResponse:
    """Authenticate by email/username + password; return a token pair."""
    from app.core.security import verify_password

    store = AuthStore()

    # Identifier may be an email or a username.
    user = store.get_by_email(req.identifier)
    if user is None:
        user = store.get_by_username(req.identifier)

    # Single generic error to avoid user enumeration.
    if user is None or not user.get("is_active", 1):
        return _err("INVALID_CREDENTIALS", "邮箱/用户名或密码错误", 401)

    # ``_row_to_user`` intentionally strips the hash, so fetch it separately.
    password_hash = store.get_password_hash(user["id"])
    if not password_hash or not verify_password(req.password, password_hash):
        return _err("INVALID_CREDENTIALS", "邮箱/用户名或密码错误", 401)

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
    return _ok({"user": current_user})
