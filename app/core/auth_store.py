"""SQLite-backed user store for JWT authentication.

Mirrors ``app.core.state_store`` (thread-local connections + WAL) but uses a
**dedicated database file** (``app/data/auth.db``) so authentication data
never mixes with graph session state in ``graph_state.db``.

All queries are parameterised (``?`` placeholders) — no string building —
to prevent SQL injection.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.utils.logger import logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "auth.db"

# Thread-local connections for async safety (separate slot from StateStore)
_local = threading.local()


def _get_conn(db_path: str = str(DB_PATH)) -> sqlite3.Connection:
    """Get a thread-local SQLite connection for auth.db."""
    if not hasattr(_local, "auth_conn") or _local.auth_conn is None:
        _local.auth_conn = sqlite3.connect(db_path, check_same_thread=False)
        _local.auth_conn.row_factory = sqlite3.Row
        _local.auth_conn.execute("PRAGMA journal_mode=WAL")
        _local.auth_conn.execute("PRAGMA busy_timeout=5000")
    return _local.auth_conn


class AuthStore:
    """Persistent user store backed by SQLite (``auth.db``)."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DB_PATH)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = _get_conn(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT UNIQUE NOT NULL,
                username      TEXT UNIQUE,
                password_hash TEXT NOT NULL,
                is_active     INTEGER NOT NULL DEFAULT 1,
                created_at    REAL NOT NULL,
                updated_at    REAL NOT NULL
            );
        """)
        conn.commit()

    # ── Create ───────────────────────────────────────────────────────────

    def create_user(self, email: str, username: str | None, password_hash: str) -> int:
        """Insert a new user; return the new auto-increment id."""
        now = time.time()
        conn = _get_conn(self.db_path)
        cur = conn.execute(
            "INSERT INTO users (email, username, password_hash, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (email, username, password_hash, now, now),
        )
        conn.commit()
        user_id = int(cur.lastrowid)
        logger.info("AuthStore: user created | id=%d | email=%s", user_id, email)
        return user_id

    # ── Reads ────────────────────────────────────────────────────────────

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a ``users`` row into a public dict (never includes hash)."""
        return {
            "id": row["id"],
            "email": row["email"],
            "username": row["username"],
            "is_active": bool(row["is_active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def get_by_email(self, email: str) -> dict[str, Any] | None:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_username(self, username: str) -> dict[str, Any] | None:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_by_id(self, user_id: int) -> dict[str, Any] | None:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._row_to_user(row) if row else None

    def get_password_hash(self, user_id: int) -> str | None:
        """Return the stored bcrypt hash for *user_id*.

        Kept deliberately separate from ``_row_to_user`` (which never exposes
        the hash) so the credential can only ever be surfaced inside the login
        flow, never through the public user dict returned by ``get_by_id`` /
        ``get_by_email`` / ``get_by_username``.
        """
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        return row["password_hash"] if row else None

    # ── Uniqueness checks ───────────────────────────────────────────────

    def email_exists(self, email: str) -> bool:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
        return row is not None

    def username_exists(self, username: str) -> bool:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        return row is not None
