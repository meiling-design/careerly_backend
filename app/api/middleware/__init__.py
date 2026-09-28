"""Authentication and rate-limiting middleware for the API.

Usage (in server.py):
    from app.api.middleware import add_api_middleware
    add_api_middleware(app)

This enables:
  1. X-API-Key header validation (opt-in via API_KEY env var)
  2. Per-IP rate limiting (opt-in via RATE_LIMIT env var)
"""

from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.status import HTTP_401_UNAUTHORIZED, HTTP_429_TOO_MANY_REQUESTS

from app.utils.logger import logger


# ── Configuration ─────────────────────────────────────────────────────────

_API_KEY = os.getenv("API_KEY", "").strip()
_RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT", "0") or "0")
_HEALTH_PATH = "/health"


# ── API Key Authentication ──────────────────────────────────────────────────


class APIKeyMiddleware(BaseHTTPMiddleware):
    """Validates X-API-Key header on every request (except /health).

    Enable by setting ``API_KEY=your-secret-key`` in the environment.
    When ``API_KEY`` is empty (default), all requests pass through.
    """

    async def dispatch(self, request: Request, call_next: Callable):
        # Skip auth for health checks
        if request.url.path == _HEALTH_PATH:
            return await call_next(request)

        # If no API key is configured, skip auth entirely
        if not _API_KEY:
            return await call_next(request)

        # Validate header
        header_key = request.headers.get("X-API-Key", "").strip()
        if not header_key:
            logger.warning("Auth rejected: missing X-API-Key | ip=%s", request.client.host if request.client else "?")
            return JSONResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                content={"detail": "Missing X-API-Key header"},
            )

        if not hmac.compare_digest(header_key, _API_KEY):
            logger.warning("Auth rejected: invalid API key | ip=%s", request.client.host if request.client else "?")
            return JSONResponse(
                status_code=HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid API key"},
            )

        return await call_next(request)


# ── Rate Limiting ──────────────────────────────────────────────────────────


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory per-IP rate limiter.

    Enable by setting ``RATE_LIMIT=<max_requests_per_minute>``.
    Default (0) means no limit.
    """

    def __init__(self, app: FastAPI, max_per_minute: int = 0):
        super().__init__(app)
        self._max = max_per_minute
        # IP → [(timestamp, count_in_window), ...]
        self._windows: dict[str, list[float]] = defaultdict(list)

    async def dispatch(self, request: Request, call_next: Callable):
        if self._max <= 0:
            return await call_next(request)

        if request.url.path == _HEALTH_PATH:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        window_start = now - 60

        # Prune stale entries and count current window
        recent = [t for t in self._windows.get(client_ip, []) if t > window_start]
        if len(recent) >= self._max:
            logger.warning("Rate limit hit | ip=%s | count=%d", client_ip, len(recent))
            return JSONResponse(
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": f"Rate limit exceeded ({self._max}/minute)"},
            )

        recent.append(now)
        self._windows[client_ip] = recent

        return await call_next(request)


# ── Factory ────────────────────────────────────────────────────────────────


def add_api_middleware(app: FastAPI) -> None:
    """Conditionally add auth + rate limiting middleware to *app*.

    Only activates protections that are explicitly configured via
    environment variables:
      - ``API_KEY`` → X-API-Key header validation
      - ``RATE_LIMIT`` → per-IP rate limiting (requests/minute)

    Both are opt-in — when neither is set the app runs unprotected
    (suitable for local dev or internal networks).
    """
    added: list[str] = []

    if _RATE_LIMIT_PER_MINUTE > 0:
        app.add_middleware(RateLimitMiddleware, max_per_minute=_RATE_LIMIT_PER_MINUTE)
        added.append(f"RateLimiter ({_RATE_LIMIT_PER_MINUTE}/min)")

    if _API_KEY:
        app.add_middleware(APIKeyMiddleware)
        added.append("APIKey")

    if added:
        logger.info("API middleware enabled: %s", ", ".join(added))
    else:
        logger.info("API middleware: none configured (open access)")
