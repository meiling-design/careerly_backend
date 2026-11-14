"""Demo usage guard — limits unauthenticated visitors to one free match.

Tracks usage by ``visitor_id`` (from ``X-Visitor-Id`` header, falling back to
the client IP). Records are stored in a lightweight SQLite table inside the
shared ``graph_state.db``.

Enabled by setting ``DEMO_MODE=true`` in the environment.

Usage::

    guard = DemoGuard()

    # Before running a match
    ok, err = await guard.check(visitor_id)
    if not ok:
        return _err(err["code"], err["message"], 429)

    # After running a match
    await guard.record_match(visitor_id, {"session_id": session_id})
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from app.utils.logger import logger
# ⚠️ 这里的 graph_state.db 是 DemoGuard 的试用计数，与引擎的
# app/data/graph_state.db **同名不同文件** —— 必须锚定遗留的 <root>/data 树。
from app.utils.paths import ROOT_DATA_DIR as DATA_DIR

DB_PATH = DATA_DIR / "graph_state.db"

# ── Environment ─────────────────────────────────────────────────────────────

import os

_DEMO_MODE = os.getenv("DEMO_MODE", "false").lower() in ("1", "true", "yes")
_MAX_DEMO_MATCHES = int(os.getenv("DEMO_MAX_MATCHES", "999"))


class DemoGuard:
    """Demo mode quota enforcer.

    Each visitor (identified by ``visitor_id``) is allowed up to
    ``DEMO_MAX_MATCHES`` (default 1) match requests.

    The guard is a no-op when ``DEMO_MODE=false``.
    """

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DB_PATH)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None

    # ── Connection ──────────────────────────────────────────────────────────

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._init_schema()
        return self._conn

    async def _init_schema(self) -> None:
        conn = self._conn
        if conn is None:
            return
        await conn.executescript("""
            CREATE TABLE IF NOT EXISTS demo_usage (
                visitor_id    TEXT PRIMARY KEY,
                match_count   INTEGER NOT NULL DEFAULT 0,
                first_match_at REAL NOT NULL,
                last_match_at  REAL NOT NULL,
                metadata_json  TEXT NOT NULL DEFAULT '{}'
            );
        """)
        await conn.commit()

    # ── Public API ──────────────────────────────────────────────────────────

    async def check(self, visitor_id: str) -> tuple[bool, Optional[dict]]:
        """Check whether *visitor_id* may run a match.

        Returns ``(True, None)`` if allowed, or ``(False, error_dict)`` if
        the demo limit has been reached.

        Always returns ``(True, None)`` when ``DEMO_MODE=false``.
        """
        if not _DEMO_MODE:
            return True, None

        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT match_count FROM demo_usage WHERE visitor_id = ?",
            (visitor_id,),
        )
        row = await cursor.fetchone()
        count = row["match_count"] if row else 0

        if count >= _MAX_DEMO_MATCHES:
            logger.info("DemoGuard: limit hit | visitor=%s | count=%d/%d",
                        visitor_id[:20], count, _MAX_DEMO_MATCHES)
            return False, {
                "code": "DEMO_LIMIT_REACHED",
                "message": (
                    f"Demo limit reached — you have used your "
                    f"{_MAX_DEMO_MATCHES} free match. "
                    f"Contact the author for full access."
                ),
            }
        return True, None

    async def record_match(self, visitor_id: str, metadata: dict | None = None) -> None:
        """Increment the match counter for *visitor_id*."""
        if not _DEMO_MODE:
            return

        now = time.time()
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT match_count FROM demo_usage WHERE visitor_id = ?",
            (visitor_id,),
        )
        row = await cursor.fetchone()
        if row:
            await conn.execute(
                "UPDATE demo_usage SET match_count = match_count + 1, "
                "last_match_at = ? WHERE visitor_id = ?",
                (now, visitor_id),
            )
        else:
            await conn.execute(
                "INSERT INTO demo_usage "
                "(visitor_id, match_count, first_match_at, last_match_at, metadata_json) "
                "VALUES (?, 1, ?, ?, ?)",
                (visitor_id, now, now, json.dumps(metadata or {})),
            )
        await conn.commit()

    async def get_usage(self, visitor_id: str) -> dict[str, Any]:
        """Return usage stats for a visitor."""
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM demo_usage WHERE visitor_id = ?", (visitor_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return {"visitor_id": visitor_id, "match_count": 0}
        return {
            "visitor_id": row["visitor_id"],
            "match_count": row["match_count"],
            "first_match_at": row["first_match_at"],
            "last_match_at": row["last_match_at"],
        }

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
