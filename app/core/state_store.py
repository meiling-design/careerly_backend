"""SQLite-based persistent State Store for LangGraph sessions.

Replaces ad-hoc JSON file checkpoints with a structured database.
Supports:
- Session CRUD (create / load / save)
- Job & resume persistence as JSON blobs
- Embedding cache (job_id → vector)
- Match results with scores
- Progress log append
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.models.job import JobList, JobPosting
from app.models.match import MatchResult
from app.models.resume import ResumeProfile
from app.utils.logger import logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "graph_state.db"

# Thread-local connections for async safety
_local = threading.local()


def _get_conn(db_path: str = str(DB_PATH)) -> sqlite3.Connection:
    """Get a thread-local SQLite connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(db_path, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


class StateStore:
    """Persistent session state backed by SQLite."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DB_PATH)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = _get_conn(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                config_json  TEXT NOT NULL DEFAULT '{}',
                error_json   TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL REFERENCES sessions(session_id),
                job_index   INTEGER NOT NULL,
                title       TEXT NOT NULL,
                company     TEXT,
                location    TEXT,
                data_json   TEXT NOT NULL,
                embedding   BLOB,
                UNIQUE(session_id, job_index)
            );

            CREATE TABLE IF NOT EXISTS resume (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL REFERENCES sessions(session_id),
                resume_json TEXT NOT NULL,
                embedding   BLOB,
                UNIQUE(session_id)
            );

            CREATE TABLE IF NOT EXISTS match_results (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL REFERENCES sessions(session_id),
                job_index   INTEGER NOT NULL,
                score       REAL NOT NULL,
                result_json TEXT NOT NULL,
                UNIQUE(session_id, job_index)
            );

            CREATE TABLE IF NOT EXISTS progress_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL REFERENCES sessions(session_id),
                seq         INTEGER NOT NULL DEFAULT 0,
                message     TEXT NOT NULL,
                created_at  REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_session ON jobs(session_id);
            CREATE INDEX IF NOT EXISTS idx_match_session ON match_results(session_id);
            CREATE INDEX IF NOT EXISTS idx_logs_session ON progress_logs(session_id);
        """)
        conn.commit()

    # ── Session lifecycle ───────────────────────────────────────────────

    def create_session(self, config: dict | None = None) -> str:
        """Create a new session and return its ID."""
        session_id = f"session-{uuid.uuid4().hex[:12]}"
        now = time.time()
        conn = _get_conn(self.db_path)
        conn.execute(
            "INSERT INTO sessions (session_id, created_at, updated_at, status, config_json) VALUES (?, ?, ?, ?, ?)",
            (session_id, now, now, "pending", json.dumps(config or {}, ensure_ascii=False)),
        )
        conn.commit()
        logger.info("StateStore: session created | id=%s", session_id)
        return session_id

    def session_exists(self, session_id: str) -> bool:
        conn = _get_conn(self.db_path)
        row = conn.execute("SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
        return row is not None

    def update_session_status(self, session_id: str, status: str) -> None:
        conn = _get_conn(self.db_path)
        conn.execute(
            "UPDATE sessions SET status = ?, updated_at = ? WHERE session_id = ?",
            (status, time.time(), session_id),
        )
        conn.commit()

    def list_sessions(self, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        """List recent sessions with metadata."""
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT session_id, status, config_json, created_at, updated_at, error_json "
            "FROM sessions ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            cfg = json.loads(row["config_json"]) if row["config_json"] else {}
            errors_list = json.loads(row["error_json"]) if row["error_json"] else []
            results.append({
                "session_id": row["session_id"],
                "status": row["status"],
                "config": cfg,
                "error_count": len(errors_list),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return results

    def find_by_idempotency_key(self, key: str) -> dict[str, Any] | None:
        """Find a session by its idempotency key (stored in config_json)."""
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT session_id, status FROM sessions WHERE config_json LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            (f"%{key}%",),
        ).fetchone()
        if not row:
            return None
        return {"session_id": row["session_id"], "status": row["status"]}

    def update_session_progress(self, session_id: str, **progress: Any) -> None:
        """Update real-time progress metadata in session config_json.

        Called from orchestrator nodes as they run, so the ``/tasks/{id}``
        endpoint can report current node and completion status.
        """
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT config_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return
        cfg = json.loads(row["config_json"]) if row["config_json"] else {}
        cfg["_progress"] = {**cfg.get("_progress", {}), **progress, "_updated_at": time.time()}
        conn.execute(
            "UPDATE sessions SET config_json = ?, updated_at = ? WHERE session_id = ?",
            (json.dumps(cfg, ensure_ascii=False), time.time(), session_id),
        )
        conn.commit()

    # ── Jobs ────────────────────────────────────────────────────────────

    def save_jobs(self, session_id: str, job_list: JobList) -> None:
        """Persist all jobs for a session."""
        conn = _get_conn(self.db_path)
        conn.execute("DELETE FROM jobs WHERE session_id = ?", (session_id,))
        rows = []
        for idx, job in enumerate(job_list.jobs):
            rows.append((
                session_id, idx, job.title, job.company, job.location,
                json.dumps(job.model_dump(), ensure_ascii=False, default=str),
            ))
        conn.executemany(
            "INSERT INTO jobs (session_id, job_index, title, company, location, data_json) VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        logger.info("StateStore: saved %d jobs for %s", len(rows), session_id)

    def load_jobs(self, session_id: str) -> JobList | None:
        """Load all jobs for a session."""
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT job_index, data_json FROM jobs WHERE session_id = ? ORDER BY job_index",
            (session_id,),
        ).fetchall()
        if not rows:
            return None
        posts = []
        for row in rows:
            data = json.loads(row["data_json"])
            posts.append(JobPosting(**data))
        return JobList(jobs=posts, total_count=len(posts), session_id=session_id)

    def save_job_embedding(self, session_id: str, job_index: int, embedding: list[float]) -> None:
        """Cache a job embedding as a binary blob."""
        import struct
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn = _get_conn(self.db_path)
        conn.execute(
            "UPDATE jobs SET embedding = ? WHERE session_id = ? AND job_index = ?",
            (blob, session_id, job_index),
        )
        conn.commit()

    def load_all_job_embeddings(self, session_id: str) -> dict[int, list[float]]:
        """Load all cached job embeddings for a session."""
        import struct
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT job_index, embedding FROM jobs WHERE session_id = ? AND embedding IS NOT NULL ORDER BY job_index",
            (session_id,),
        ).fetchall()
        result: dict[int, list[float]] = {}
        for row in rows:
            if row["embedding"]:
                n = len(row["embedding"]) // 4
                fmt = f"{n}f"
                result[row["job_index"]] = list(struct.unpack(fmt, row["embedding"]))
        return result

    # ── Resume ──────────────────────────────────────────────────────────

    def save_resume(self, session_id: str, profile: ResumeProfile) -> None:
        conn = _get_conn(self.db_path)
        conn.execute("DELETE FROM resume WHERE session_id = ?", (session_id,))
        conn.execute(
            "INSERT INTO resume (session_id, resume_json) VALUES (?, ?)",
            (session_id, json.dumps(profile.model_dump(), ensure_ascii=False, default=str)),
        )
        conn.commit()
        logger.info("StateStore: saved resume for %s", session_id)

    def load_resume(self, session_id: str) -> ResumeProfile | None:
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT resume_json FROM resume WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row and row["resume_json"]:
            return ResumeProfile(**json.loads(row["resume_json"]))
        return None

    def save_resume_embedding(self, session_id: str, embedding: list[float]) -> None:
        import struct
        blob = struct.pack(f"{len(embedding)}f", *embedding)
        conn = _get_conn(self.db_path)
        conn.execute(
            "UPDATE resume SET embedding = ? WHERE session_id = ?",
            (blob, session_id),
        )
        conn.commit()

    def load_resume_embedding(self, session_id: str) -> list[float] | None:
        import struct
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT embedding FROM resume WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row and row["embedding"]:
            n = len(row["embedding"]) // 4
            return list(struct.unpack(f"{n}f", row["embedding"]))
        return None

    # ── Match Results ───────────────────────────────────────────────────

    def save_match_results(self, session_id: str, results: list[MatchResult]) -> None:
        conn = _get_conn(self.db_path)
        conn.execute("DELETE FROM match_results WHERE session_id = ?", (session_id,))
        rows = []
        for job_idx, r in enumerate(results):
            rows.append((
                session_id, job_idx, r.score,
                json.dumps(r.model_dump(), ensure_ascii=False, default=str),
            ))
        conn.executemany(
            "INSERT INTO match_results (session_id, job_index, score, result_json) VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        logger.info("StateStore: saved %d match results for %s", len(rows), session_id)

    def load_match_results(self, session_id: str) -> list[MatchResult] | None:
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT result_json FROM match_results WHERE session_id = ? ORDER BY job_index",
            (session_id,),
        ).fetchall()
        if not rows:
            return None
        from app.models.match import MatchResult as MR
        return [MR(**json.loads(row["result_json"])) for row in rows]

    # ── Progress Logs ───────────────────────────────────────────────────

    def append_log(self, session_id: str, message: str) -> None:
        conn = _get_conn(self.db_path)
        max_seq = conn.execute(
            "SELECT COALESCE(MAX(seq), -1) as m FROM progress_logs WHERE session_id = ?",
            (session_id,),
        ).fetchone()["m"]
        conn.execute(
            "INSERT INTO progress_logs (session_id, seq, message, created_at) VALUES (?, ?, ?, ?)",
            (session_id, max_seq + 1, message, time.time()),
        )
        conn.commit()

    def load_logs(self, session_id: str, limit: int = 100) -> list[str]:
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT message FROM progress_logs WHERE session_id = ? ORDER BY seq DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
        return [row["message"] for row in reversed(rows)]

    # ── Session errors ──────────────────────────────────────────────────

    def append_error(self, session_id: str, error: str) -> None:
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT error_json FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row:
            errors = json.loads(row["error_json"])
            errors.append(error)
            conn.execute(
                "UPDATE sessions SET error_json = ?, updated_at = ? WHERE session_id = ?",
                (json.dumps(errors, ensure_ascii=False), time.time(), session_id),
            )
            conn.commit()

    def load_errors(self, session_id: str) -> list[str]:
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT error_json FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row:
            return json.loads(row["error_json"])
        return []

    # ── Full save/load ──────────────────────────────────────────────────

    def save_state(
        self,
        session_id: str,
        *,
        jobs: JobList | None = None,
        resume: ResumeProfile | None = None,
        match_results: list[MatchResult] | None = None,
        errors: list[str] | None = None,
        status: str | None = None,
        config: dict | None = None,
    ) -> None:
        """Atomic save of multiple state components."""
        if not self.session_exists(session_id):
            self.create_session(config or {})

        if jobs is not None:
            self.save_jobs(session_id, jobs)
        if resume is not None:
            self.save_resume(session_id, resume)
        if match_results is not None:
            self.save_match_results(session_id, match_results)
        if errors:
            for e in errors:
                self.append_error(session_id, e)
        if status:
            self.update_session_status(session_id, status)

    def load_state(self, session_id: str) -> dict[str, Any]:
        """Load all persisted state for a session."""
        return {
            "jobs": self.load_jobs(session_id),
            "resume": self.load_resume(session_id),
            "match_results": self.load_match_results(session_id),
            "logs": self.load_logs(session_id),
            "errors": self.load_errors(session_id),
        }

    # ── Session info ────────────────────────────────────────────────────

    def get_session_info(self, session_id: str) -> dict[str, Any] | None:
        """Return session metadata (status, config, timestamps) or None."""
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT session_id, status, config_json, created_at, updated_at, error_json "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "session_id": row["session_id"],
            "status": row["status"],
            "config": json.loads(row["config_json"]) if row["config_json"] else {},
            "errors": json.loads(row["error_json"]) if row["error_json"] else [],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ── GraphState restoration (session resume) ─────────────────────────

    def restore_graph_state(self) -> dict[str, Any]:
        """Build a partial GraphState dict from persisted data.

        Returns an updates dict that can be merged into a fresh GraphState
        to resume from where a prior run left off.
        """
        # This is a placeholder — the caller passes session_id at restore time.
        # The actual method with session_id parameter is on state_store_ext.
        pass


# ── Helper: reconstruct GraphState from stored session ──────────────────────


def restore_session_state(store: StateStore, session_id: str) -> dict[str, Any]:
    """Load persisted session data and return a dict that can merge into GraphState.

    Usage:
        store = StateStore()
        updates = restore_session_state(store, session_id)
        state = GraphState(**updates)  # plus fresh run params
    """
    from app.models.job import JobList
    from app.models.match import MatchResult
    from app.models.resume import ResumeProfile

    info = store.get_session_info(session_id)
    if not info:
        return {}

    updates: dict[str, Any] = {
        "session_id": session_id,
        "errors": list(info["errors"]),
    }

    # Restore jobs → crawler_output
    jobs = store.load_jobs(session_id)
    if jobs:
        updates["crawler_output"] = jobs
        updates["normalized_jobs"] = jobs

    # Restore resume
    resume = store.load_resume(session_id)
    if resume:
        updates["resume_output"] = resume

    # Restore match_results
    match_results = store.load_match_results(session_id)
    if match_results:
        updates["match_results"] = match_results

    # Restore config flags from session metadata
    cfg = info.get("config", {})
    if isinstance(cfg, dict):
        updates["use_llm"] = cfg.get("use_llm", True)
        updates["use_embedding"] = cfg.get("use_embedding", True)

    # Restore logs
    logs = store.load_logs(session_id)
    if logs:
        updates["progress_log"] = logs

    return updates
