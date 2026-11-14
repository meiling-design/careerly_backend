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
# 数据根由 app.utils.paths 统一给出（可被 DATA_DIR 重定向到数据盘）。
# 本地名保持 DATA_DIR —— 测试靠 monkeypatch.setattr(模块, "DATA_DIR"/"DB_PATH", …) 隔离。
from app.utils.paths import APP_DATA_DIR as DATA_DIR

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

        # ── Migration: sessions.user_id（架构决策 D-F / P0-4）────────────────
        # 归属列只存在于**持久层**：由 API 层写入，引擎（LangGraph / GraphState）
        # 对它**无感知** —— 见 create_session 的说明。
        #
        # CREATE TABLE IF NOT EXISTS 不会为既有库补列，故先探后 ALTER。
        # 必须放在建索引之前，否则 idx_sessions_user 会因列不存在而失败。
        #
        # 历史行的 user_id 保持 NULL：`WHERE user_id = ?` 永不匹配 NULL
        # ⇒ 旧 session 对**任意**用户都不可见（fail-closed，无需清理脚本）。
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        if existing_cols and "user_id" not in existing_cols:
            conn.execute("ALTER TABLE sessions ADD COLUMN user_id TEXT")
            logger.info("StateStore: migrated sessions table — added user_id column")

        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id   TEXT PRIMARY KEY,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL,
                status       TEXT NOT NULL DEFAULT 'pending',
                config_json  TEXT NOT NULL DEFAULT '{}',
                error_json   TEXT NOT NULL DEFAULT '[]',
                user_id      TEXT
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

            -- 归属查询索引（P0-4）。历史行 user_id 为 NULL，索引对它们无害。
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
        """)
        conn.commit()

    # ── Session lifecycle ───────────────────────────────────────────────

    @staticmethod
    def new_session_id() -> str:
        """按本 store 的命名约定生成一个 session id，**不落库**。

        P1-1b：API 层需要「先生成 id、交给引擎、同时写进自己的任务行」这个顺序，
        故把命名规则单独暴露出来 —— 免得调用方各自拼一个 ``session-`` 前缀，
        两处格式一旦漂移，绑定就静默失效。
        """
        return f"session-{uuid.uuid4().hex[:12]}"

    def create_session(
        self,
        config: dict | None = None,
        user_id: str | int | None = None,
        session_id: str | None = None,
    ) -> str:
        """Create a new session and return its ID.

        ``session_id``（P1-1b）—— 调用方指定 id，而不是自动生成。
        用于把引擎 session 与 Careerly 的 ``analysis_tasks`` 行**预先绑定**：
        API 层先生成 id 写进任务行，再把它交给引擎，于是运行期间
        ``GET /analysis/tasks/{id}`` 就能读到本 session 的节点进度。
        不传则沿用 ``session-<hex>`` 的自动命名（既有调用方零改动）。

        架构决策 D-F（2026-09-30 裁决）—— ``user_id`` 的边界：
            持久层（本表）**有** ``user_id``，用于数据归属与用户隔离；
            引擎层（``GraphState`` / LangGraph 节点）**不携带** ``user_id``，
            以保持引擎逻辑纯粹、与产品身份解耦。
            写入由 **API 层**负责（见 ``router_v1.POST /match``）。
            引擎内部 / 离线调用（``scripts/e2e_careerly.py`` 等）不传该参数，
            产生的行为 NULL，对**任何**用户查询均不可见（fail-closed）。
        """
        session_id = session_id or self.new_session_id()
        now = time.time()
        conn = _get_conn(self.db_path)
        conn.execute(
            "INSERT INTO sessions (session_id, created_at, updated_at, status, config_json, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                session_id,
                now,
                now,
                "pending",
                json.dumps(config or {}, ensure_ascii=False),
                None if user_id is None else str(user_id),
            ),
        )
        conn.commit()
        logger.info("StateStore: session created | id=%s | user=%s", session_id, user_id)
        return session_id

    def set_session_owner_if_unowned(self, session_id: str, user_id: str | int) -> bool:
        """Claim an **unowned** session for ``user_id``; never steals ownership.

        用于引擎自行创建 session 的场景（根路径 ``POST /match``）：引擎不感知
        用户身份，故由 API 层在运行结束后补写归属。``AND user_id IS NULL``
        保证不会覆盖他人归属 —— 若该 session 已属他人，本次调用静默无效
        （调用方随后也读不到它，符合 fail-closed）。

        Returns:
            True 表示本次调用完成了归属写入。
        """
        conn = _get_conn(self.db_path)
        cur = conn.execute(
            "UPDATE sessions SET user_id = ?, updated_at = ? "
            "WHERE session_id = ? AND user_id IS NULL",
            (str(user_id), time.time(), session_id),
        )
        conn.commit()
        if cur.rowcount:
            logger.info("StateStore: session ownership claimed | id=%s | user=%s", session_id, user_id)
        return bool(cur.rowcount)

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

    def list_sessions(
        self,
        limit: int = 20,
        offset: int = 0,
        user_id: str | int | None = None,
    ) -> list[dict[str, Any]]:
        """List recent sessions with metadata.

        ⚠️ 归属纪律（决策 D-F）：**HTTP 路由必须传 user_id**。
        ``user_id=None`` 表示「不做归属过滤」，仅供引擎 / 离线脚本使用。
        ``WHERE user_id = ?`` 不会匹配历史 NULL 行 —— 这是刻意的 fail-closed，
        **不要**改写成 ``WHERE user_id IS ?`` 或 ``OR user_id IS NULL``。
        """
        conn = _get_conn(self.db_path)
        if user_id is None:
            sql = (
                "SELECT session_id, status, config_json, created_at, updated_at, error_json "
                "FROM sessions ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            params: tuple[Any, ...] = (limit, offset)
        else:
            sql = (
                "SELECT session_id, status, config_json, created_at, updated_at, error_json "
                "FROM sessions WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?"
            )
            params = (str(user_id), limit, offset)
        rows = conn.execute(sql, params).fetchall()
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

    def find_by_idempotency_key(
        self,
        key: str,
        user_id: str | int | None = None,
    ) -> dict[str, Any] | None:
        """Find a session by its idempotency key (stored in config_json).

        ⚠️ 归属纪律（决策 D-F）：HTTP 路由必须传 user_id。
        否则当两个用户提交**完全相同**的简历 + 岗位组合（幂等键相同）时，
        A 会拿到 B 的 session_id —— 这是一条真实的越权路径，
        不只是幂等语义问题。
        """
        conn = _get_conn(self.db_path)
        if user_id is None:
            row = conn.execute(
                "SELECT session_id, status FROM sessions WHERE config_json LIKE ? "
                "ORDER BY created_at DESC LIMIT 1",
                (f"%{key}%",),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT session_id, status FROM sessions "
                "WHERE user_id = ? AND config_json LIKE ? ORDER BY created_at DESC LIMIT 1",
                (str(user_id), f"%{key}%"),
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

    def get_session_progress(self, session_id: str) -> dict[str, Any]:
        """Read back what :meth:`update_session_progress` last wrote.

        Returns ``{}`` when the session is unknown, has no ``config_json``,
        or never reported progress — callers treat that as "排队中/未知",
        never as "进度为 0%。"
        """
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT config_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row or not row["config_json"]:
            return {}
        try:
            cfg = json.loads(row["config_json"])
        except (TypeError, ValueError):
            return {}
        progress = cfg.get("_progress") if isinstance(cfg, dict) else None
        return progress if isinstance(progress, dict) else {}

    def update_session_config(self, session_id: str, config: dict) -> None:
        """Replace the entire session config_json."""
        conn = _get_conn(self.db_path)
        conn.execute(
            "UPDATE sessions SET config_json = ?, updated_at = ? WHERE session_id = ?",
            (json.dumps(config, ensure_ascii=False), time.time(), session_id),
        )
        conn.commit()

    def update_session_config_key(self, session_id: str, key: str, value: Any) -> None:
        """Set or delete (if value is None) a single key in config_json."""
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT config_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return
        cfg = json.loads(row["config_json"]) if row["config_json"] else {}
        if value is None:
            cfg.pop(key, None)
        else:
            cfg[key] = value
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

    def get_session_info(
        self,
        session_id: str,
        user_id: str | int | None = None,
    ) -> dict[str, Any] | None:
        """Return session metadata (status, config, timestamps) or None.

        ⚠️ 归属纪律（决策 D-F）：**HTTP 路由必须传 user_id**。
        传了 user_id 但归属不符时返回 ``None`` —— 调用方据此返回 404，
        与「资源不存在」不可区分（决策 D-G：不泄露资源存在性，防 id 枚举）。
        ``user_id=None`` 表示不做归属过滤，仅供引擎 / 离线脚本使用。
        """
        conn = _get_conn(self.db_path)
        if user_id is None:
            row = conn.execute(
                "SELECT session_id, status, config_json, created_at, updated_at, error_json "
                "FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT session_id, status, config_json, created_at, updated_at, error_json "
                "FROM sessions WHERE session_id = ? AND user_id = ?",
                (session_id, str(user_id)),
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
