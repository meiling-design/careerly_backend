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
* ``REFRESH_TOKEN_EXPIRE_DAYS``   Lifetime of refresh tokens (default 7).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.utils.logger import logger

# ── Secrets & token configuration ─────────────────────────────────────────

SECRET_KEY = os.getenv("JWT_SECRET_KEY", "dev-insecure-change-me")
if SECRET_KEY == "dev-insecure-change-me":
    logger.warning(
        "JWT_SECRET_KEY not set — using insecure dev default. "
        "Set JWT_SECRET_KEY in production to a long random string."
    )

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.getenv("REFRESH_TOKEN_EXPIRE_DAYS", "7"))

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

def _create_token(subject: str, token_type: str, expires_delta: timedelta) -> str:
    """Build and sign a JWT for *subject* of the given *token_type*."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_access_token(subject: str) -> str:
    """Create a short-lived access token (used as the Bearer credential)."""
    return _create_token(subject, _ACCESS_TYPE, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))


def create_refresh_token(subject: str) -> str:
    """Create a long-lived refresh token (used to mint new access tokens)."""
    return _create_token(subject, _REFRESH_TYPE, timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS))


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
    return user


# ── Token issuance helper ─────────────────────────────────────────────────

def issue_tokens(user_dict: dict) -> dict:
    """Build a token bundle for *user_dict*.

    Returns ``{"access_token", "refresh_token", "token_type": "bearer",
    "expires_in": <seconds>}``.
    """
    subject = str(user_dict["id"])
    return {
        "access_token": create_access_token(subject),
        "refresh_token": create_refresh_token(subject),
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }
