"""Domain-level errors that map onto the unified response envelope.

由 ``app/api/server.py`` 注册的全局异常处理器统一转成::

    {"success": false, "data": null,
     "error": {"code": ..., "message": ..., "details"?: ...},
     "meta": {"timestamp": ..., "version": "v1"}}

────────────────────────────────────────────────────────────────────────────
⚠️ 设计决策 D-G（2026-09-30 产品侧裁决）—— 越权错误码统一为 404
────────────────────────────────────────────────────────────────────────────
``PermissionDenied`` 的 ``status_code`` 就是 **404**、``code`` 就是 **NOT_FOUND**，
与「资源真的不存在」**完全不可区分**。

这是**故意**的设计，不是笔误：403 会向调用方泄露
「该 id 有效，只是你没权限」这一事实，攻击者可据此**枚举**他人资源。
前端也**不需要**区分这两种情况 —— 对用户而言都是「拿不到这份数据」。

**不要"顺手修正"成 403。** 若确有需要向用户区分「无权」，应走产品层文案，
而不是靠 HTTP 状态码。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for errors carrying an HTTP status + envelope code."""

    status_code: int = 400
    code: str = "APP_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details = details

    def to_error_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            payload["details"] = self.details
        return payload


class ResourceNotFound(AppError):
    """The resource genuinely does not exist (or the caller may not see it)."""

    status_code = 404
    code = "NOT_FOUND"


class PermissionDenied(AppError):
    """The resource exists but is **not owned** by the caller.

    ⚠️ 决策 D-G：对外一律呈现为 **404 + NOT_FOUND**，与「真的不存在」不可区分。
    见模块文档字符串 —— 这是安全要求，不是实现疏漏。
    """

    status_code = 404
    code = "NOT_FOUND"


class Unauthenticated(AppError):
    """No usable credentials were presented (missing / malformed bearer token)."""

    status_code = 401
    code = "UNAUTHENTICATED"


class QuotaExceeded(AppError):
    """A metered resource has been consumed (free-tier analysis quota, etc.)."""

    status_code = 403
    code = "QUOTA_EXCEEDED"


__all__ = [
    "AppError",
    "ResourceNotFound",
    "PermissionDenied",
    "Unauthenticated",
    "QuotaExceeded",
]
