"""Shared response envelope helpers.

Wire format (identical to ``router_v1`` / ``router_auth``):

    {"success": bool, "data": ..., "error": {code, message, details?} | None,
     "meta": {timestamp, version: "v1"}}

``router_v1`` and ``router_auth`` keep their own local copies for historical
reasons; **new** routers should import from here instead of copying again.
Unifying the older two is a separate, behaviour-neutral refactor.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi.responses import JSONResponse

API_VERSION = "v1"


def ok(data: Any = None, status_code: int = 200, meta_extra: dict | None = None) -> JSONResponse:
    """Success envelope."""
    meta: dict[str, Any] = {"timestamp": time.time(), "version": API_VERSION}
    if meta_extra:
        meta.update(meta_extra)
    return JSONResponse(
        status_code=status_code,
        content={"success": True, "data": data, "error": None, "meta": meta},
    )


def err(
    code: str,
    message: str,
    status_code: int = 400,
    details: Any = None,
) -> JSONResponse:
    """Error envelope (returned, not raised)."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": error,
            "meta": {"timestamp": time.time(), "version": API_VERSION},
        },
    )
