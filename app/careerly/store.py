"""SQLite persistence for the Careerly tables.

Separate database file (``app/data/careerly.db``) so Careerly's versioned
analysis data never mixes with ``graph_state.db`` (engine sessions) or
``auth.db`` (accounts).

Design notes
------------
* Analysis is **versioned, never overwritten** (Baseline §7.4 / PRD §9.7):
  ``analysis_results`` rows are appended, and ``version`` increases.
* ``jobs`` are reusable entities; ``project_jobs`` only *references* them, so
  removing a job from a project never deletes the job record.
* Every table is scoped by ``user_id`` — this is the multi-tenancy that the
  engine's own ``sessions`` table lacks (conflict C5).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.careerly.schemas import (
    AnalysisProject,
    AnalysisResult,
    GapDetail,
    JobMatch,
    JobModel,
    QuotaState,
    StrengthScore,
    iso,
)
from app.utils.logger import logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "careerly.db"

_local = threading.local()


def _get_conn(db_path: str = str(DB_PATH)) -> sqlite3.Connection:
    if not hasattr(_local, "careerly_conn") or _local.careerly_conn is None:
        _local.careerly_conn = sqlite3.connect(db_path, check_same_thread=False)
        _local.careerly_conn.row_factory = sqlite3.Row
        _local.careerly_conn.execute("PRAGMA journal_mode=WAL")
        _local.careerly_conn.execute("PRAGMA busy_timeout=5000")
    return _local.careerly_conn


def _j(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _unjson(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


class CareerlyStore:
    """Persistent store for Careerly analysis / resume / quota data."""

    def __init__(self, db_path: str | Path | None = None):
        self.db_path = str(db_path or DB_PATH)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = _get_conn(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS analysis_projects (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                name       TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS jobs (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                title      TEXT NOT NULL DEFAULT '',
                company    TEXT NOT NULL DEFAULT '',
                raw_jd     TEXT NOT NULL DEFAULT '',
                source     TEXT,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS project_jobs (
                project_id TEXT NOT NULL,
                job_id     TEXT NOT NULL,
                position   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (project_id, job_id)
            );

            CREATE TABLE IF NOT EXISTS analysis_results (
                id                  TEXT PRIMARY KEY,
                project_id          TEXT NOT NULL,
                version             INTEGER NOT NULL,
                resume_version_id   TEXT,
                capability_snapshot TEXT,
                created_at          REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS job_matches (
                result_id      TEXT NOT NULL,
                job_id         TEXT NOT NULL,
                rank           INTEGER NOT NULL DEFAULT 0,
                score          INTEGER NOT NULL DEFAULT 0,
                strengths      TEXT,
                gaps           TEXT,
                reasons        TEXT,
                suggestion     TEXT,
                strength_scores TEXT,
                gap_details    TEXT,
                PRIMARY KEY (result_id, job_id)
            );

            CREATE TABLE IF NOT EXISTS careerly_resumes (
                user_id    INTEGER PRIMARY KEY,
                text       TEXT NOT NULL DEFAULT '',
                profile    TEXT,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_quota (
                user_id   INTEGER PRIMARY KEY,
                total     INTEGER NOT NULL DEFAULT 3,
                used      INTEGER NOT NULL DEFAULT 0,
                is_member INTEGER NOT NULL DEFAULT 0
            );

            -- P1-1a：分析任务的运行记录。
            -- ⚠️ 这是「这一次分析跑到哪了」的可观测记录，不是业务数据本体
            -- （业务本体在 analysis_projects）。D-A ②双模的第一阶段：
            -- 对外 POST /analysis 仍同步返回结果，仅内部留痕。
            CREATE TABLE IF NOT EXISTS analysis_tasks (
                id            TEXT PRIMARY KEY,
                user_id       INTEGER NOT NULL,
                project_id    TEXT,               -- 成功后回填，失败为 NULL
                status        TEXT NOT NULL,      -- queued|processing|completed|failed
                title         TEXT NOT NULL DEFAULT '',
                source        TEXT NOT NULL DEFAULT 'paste',
                error_code    TEXT,               -- status=failed 时
                error_message TEXT,
                created_at    REAL NOT NULL,
                started_at    REAL,
                finished_at   REAL
            );

            -- P2-1：能力快照 —— 成长的参照系。每次分析成功写一条，
            -- 「我现在是什么样」只追加、不修改（与分析版本化同一哲学）。
            -- scores JSON 结构：{"capabilities": {name: score}, "matchScore": int|null}
            CREATE TABLE IF NOT EXISTS capability_snapshots (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                source     TEXT NOT NULL DEFAULT 'analysis',
                source_id  TEXT,                -- source=analysis 时为 project_id
                scores     TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL
            );

            -- P2-2：成长事件 —— 由相邻两条快照 diff 派生（growth.py），
            -- 不存冗余状态字段；type 必须是 Dart enum 名
            -- （experience|skill|capability|matchScore|rank），前端 tryParse 直读。
            CREATE TABLE IF NOT EXISTS growth_events (
                id          TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                type        TEXT NOT NULL,
                title       TEXT NOT NULL DEFAULT '',
                description TEXT,
                delta       INTEGER,
                occurred_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_projects_user ON analysis_projects(user_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
            CREATE INDEX IF NOT EXISTS idx_results_project ON analysis_results(project_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_user ON analysis_tasks(user_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON analysis_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_snapshots_user ON capability_snapshots(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_growth_user ON growth_events(user_id, occurred_at);
        """)
        conn.commit()

    # ── Projects ────────────────────────────────────────────────────────

    def save_project(self, user_id: int, project: AnalysisProject) -> None:
        """Insert a project with its jobs, results and matches (one transaction)."""
        conn = _get_conn(self.db_path)
        now = time.time()
        created = self._parse_ts(project.createdAt) or now
        updated = self._parse_ts(project.updatedAt) or created

        conn.execute(
            "INSERT OR REPLACE INTO analysis_projects (id, user_id, name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project.id, user_id, project.name, created, updated),
        )

        for pos, job in enumerate(project.jobs):
            conn.execute(
                "INSERT OR REPLACE INTO jobs (id, user_id, title, company, raw_jd, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job.id, user_id, job.title, job.company, job.rawJd, job.source,
                 self._parse_ts(job.createdAt) or now),
            )
            conn.execute(
                "INSERT OR REPLACE INTO project_jobs (project_id, job_id, position) VALUES (?, ?, ?)",
                (project.id, job.id, pos),
            )

        for result in project.results:
            conn.execute(
                "INSERT OR REPLACE INTO analysis_results "
                "(id, project_id, version, resume_version_id, capability_snapshot, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (result.id, result.projectId or project.id, result.version,
                 result.resumeVersionId, _j(result.capabilitySnapshot),
                 self._parse_ts(result.createdAt) or now),
            )
            for match in result.matches:
                conn.execute(
                    "INSERT OR REPLACE INTO job_matches "
                    "(result_id, job_id, rank, score, strengths, gaps, reasons, suggestion, "
                    " strength_scores, gap_details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (result.id, match.jobId, match.rank, match.score,
                     _j(match.strengths), _j(match.gaps), _j(match.reasons),
                     match.suggestion,
                     _j([s.model_dump() for s in match.strengthScores]),
                     _j([g.model_dump() for g in match.gapDetails])),
                )

        conn.commit()
        logger.info("CareerlyStore: project saved | id=%s | user=%d", project.id, user_id)

    def list_projects(self, user_id: int, limit: int = 50) -> list[AnalysisProject]:
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT * FROM analysis_projects WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [self._load_project(user_id, r) for r in rows]

    def get_project(self, user_id: int, project_id: str) -> Optional[AnalysisProject]:
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM analysis_projects WHERE id = ? AND user_id = ?",
            (project_id, user_id),
        ).fetchone()
        if row is None:
            return None
        return self._load_project(user_id, row)

    def _load_project(self, user_id: int, row: sqlite3.Row) -> AnalysisProject:
        conn = _get_conn(self.db_path)
        pid = row["id"]

        job_rows = conn.execute(
            "SELECT j.* FROM jobs j JOIN project_jobs pj ON pj.job_id = j.id "
            "WHERE pj.project_id = ? AND j.user_id = ? ORDER BY pj.position",
            (pid, user_id),
        ).fetchall()
        jobs = [
            JobModel(
                id=r["id"], title=r["title"], company=r["company"], rawJd=r["raw_jd"],
                source=r["source"], createdAt=iso(r["created_at"]),
            )
            for r in job_rows
        ]

        results: list[AnalysisResult] = []
        for r in conn.execute(
            "SELECT * FROM analysis_results WHERE project_id = ? ORDER BY version", (pid,)
        ).fetchall():
            matches = [
                JobMatch(
                    jobId=m["job_id"], score=m["score"], rank=m["rank"],
                    strengths=_unjson(m["strengths"], []),
                    gaps=_unjson(m["gaps"], []),
                    reasons=_unjson(m["reasons"], []),
                    suggestion=m["suggestion"],
                    strengthScores=[StrengthScore(**s) for s in _unjson(m["strength_scores"], [])],
                    gapDetails=[GapDetail(**g) for g in _unjson(m["gap_details"], [])],
                )
                for m in conn.execute(
                    "SELECT * FROM job_matches WHERE result_id = ? ORDER BY rank", (r["id"],)
                ).fetchall()
            ]
            results.append(AnalysisResult(
                id=r["id"], projectId=r["project_id"], version=r["version"],
                resumeVersionId=r["resume_version_id"],
                matches=matches,
                capabilitySnapshot=_unjson(r["capability_snapshot"], {}),
                createdAt=iso(r["created_at"]),
            ))

        return AnalysisProject(
            id=pid, name=row["name"], jobs=jobs, results=results,
            createdAt=iso(row["created_at"]), updatedAt=iso(row["updated_at"]),
        )

    # ── Job library ─────────────────────────────────────────────────────

    def list_jobs(self, user_id: int, limit: int = 100) -> list[JobModel]:
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT * FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [
            JobModel(id=r["id"], title=r["title"], company=r["company"], rawJd=r["raw_jd"],
                     source=r["source"], createdAt=iso(r["created_at"]))
            for r in rows
        ]

    # ── Resume baseline ─────────────────────────────────────────────────

    def save_resume(self, user_id: int, text: str, profile: dict | None = None) -> None:
        conn = _get_conn(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO careerly_resumes (user_id, text, profile, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, text, _j(profile or {}), time.time()),
        )
        conn.commit()

    def get_resume(self, user_id: int) -> Optional[dict[str, Any]]:
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM careerly_resumes WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "text": row["text"],
            "profile": _unjson(row["profile"], {}),
            "updatedAt": iso(row["updated_at"]),
        }

    # ── Quota ───────────────────────────────────────────────────────────

    FREE_ANALYSIS_QUOTA = 3  # Baseline §5 / PRD §16

    def get_quota(self, user_id: int) -> QuotaState:
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM user_quota WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return QuotaState(total=self.FREE_ANALYSIS_QUOTA, used=0, isMember=False)
        return QuotaState(total=row["total"], used=row["used"], isMember=bool(row["is_member"]))

    def consume_quota(self, user_id: int) -> None:
        """Deduct one analysis. Only call AFTER a successful analysis."""
        conn = _get_conn(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO user_quota (user_id, total, used, is_member) VALUES (?, ?, 0, 0)",
            (user_id, self.FREE_ANALYSIS_QUOTA),
        )
        conn.execute(
            "UPDATE user_quota SET used = used + 1 WHERE user_id = ? AND is_member = 0",
            (user_id,),
        )
        conn.commit()

    # ── Analysis tasks (P1-1a) ──────────────────────────────────────────
    #
    # 状态机：queued → processing → completed | failed
    #
    # 设计约束：
    #  - 任务记录**只服务可观测**，不作为业务真相；结果仍以 analysis_projects 为准。
    #  - 每个方法都带 user_id 或不带（内部流转用 task_id），但**读**一律按 user 作用域。
    #  - 失败必须落 error_code / error_message —— 「失败无痕」是 P1-2 要消灭的对象。

    TASK_QUEUED = "queued"
    TASK_PROCESSING = "processing"
    TASK_COMPLETED = "completed"
    TASK_FAILED = "failed"

    def create_task(self, user_id: int, title: str, source: str = "paste") -> str:
        """Insert a ``queued`` task and return its id."""
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        conn = _get_conn(self.db_path)
        conn.execute(
            "INSERT INTO analysis_tasks (id, user_id, project_id, status, title, source, "
            " error_code, error_message, created_at, started_at, finished_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, NULL, NULL, ?, NULL, NULL)",
            (task_id, user_id, self.TASK_QUEUED, title.strip(), source, time.time()),
        )
        conn.commit()
        return task_id

    def start_task(self, task_id: str) -> None:
        conn = _get_conn(self.db_path)
        conn.execute(
            "UPDATE analysis_tasks SET status = ?, started_at = ? "
            "WHERE id = ? AND status = ?",
            (self.TASK_PROCESSING, time.time(), task_id, self.TASK_QUEUED),
        )
        conn.commit()

    def complete_task(self, task_id: str, project_id: str) -> None:
        conn = _get_conn(self.db_path)
        conn.execute(
            "UPDATE analysis_tasks SET status = ?, project_id = ?, finished_at = ?, "
            " error_code = NULL, error_message = NULL WHERE id = ?",
            (self.TASK_COMPLETED, project_id, time.time(), task_id),
        )
        conn.commit()

    def fail_task(self, task_id: str, error_code: str, error_message: str) -> None:
        conn = _get_conn(self.db_path)
        conn.execute(
            "UPDATE analysis_tasks SET status = ?, finished_at = ?, error_code = ?, "
            " error_message = ? WHERE id = ?",
            (self.TASK_FAILED, time.time(), error_code, error_message[:2000], task_id),
        )
        conn.commit()

    def get_task(self, user_id: int, task_id: str) -> Optional[dict[str, Any]]:
        """Ownership-scoped read. Never filter by ``OR user_id IS NULL``."""
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM analysis_tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        ).fetchone()
        return self._task_row(row) if row else None

    def list_tasks(self, user_id: int, limit: int = 50) -> list[dict[str, Any]]:
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT * FROM analysis_tasks WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, min(max(limit, 1), 200)),
        ).fetchall()
        return [self._task_row(r) for r in rows]

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        started = row["started_at"]
        finished = row["finished_at"]
        duration_ms = (
            int((finished - started) * 1000) if started and finished else None
        )
        return {
            "taskId": row["id"],
            "status": row["status"],
            "projectId": row["project_id"],
            "title": row["title"],
            "source": row["source"],
            "errorCode": row["error_code"],
            "errorMessage": row["error_message"],
            "createdAt": iso(row["created_at"]),
            "startedAt": iso(started) if started else None,
            "finishedAt": iso(finished) if finished else None,
            "durationMs": duration_ms,
        }

    # ── Capability snapshots & growth events (P2) ───────────────────────
    #
    # 快照是「写一次、永不改」的参照系；事件是 diff 的**产物**。
    # 派生逻辑在 app/careerly/growth.py —— store 只负责持久化与按 user 作用域读。

    def save_snapshot(self, user_id: int, source: str, source_id: Optional[str],
                      scores: dict[str, Any]) -> str:
        """Append one immutable snapshot; returns its id."""
        snapshot_id = f"snap_{uuid.uuid4().hex[:16]}"
        conn = _get_conn(self.db_path)
        conn.execute(
            "INSERT INTO capability_snapshots (id, user_id, source, source_id, scores, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (snapshot_id, user_id, source, source_id, _j(scores), time.time()),
        )
        conn.commit()
        return snapshot_id

    def get_latest_snapshot(self, user_id: int) -> Optional[dict[str, Any]]:
        """The user's most recent snapshot (the diff baseline), or None."""
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM capability_snapshots WHERE user_id = ? "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "source": row["source"],
            "sourceId": row["source_id"],
            "scores": _unjson(row["scores"], {}),
            "createdAt": iso(row["created_at"]),
        }

    def save_growth_events(self, user_id: int, events: list[dict[str, Any]]) -> None:
        """Persist derived events. ``events`` items: type/title/description/delta/occurred_at(float)."""
        if not events:
            return
        conn = _get_conn(self.db_path)
        for ev in events:
            conn.execute(
                "INSERT INTO growth_events (id, user_id, type, title, description, delta, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"gev_{uuid.uuid4().hex[:16]}", user_id, ev["type"], ev["title"],
                 ev.get("description"), ev.get("delta"), ev["occurred_at"]),
            )
        conn.commit()

    def list_growth_events(self, user_id: int, type: Optional[str] = None,
                           limit: int = 50) -> list[dict[str, Any]]:
        conn = _get_conn(self.db_path)
        limit = min(max(limit, 1), 200)
        if type:
            rows = conn.execute(
                "SELECT * FROM growth_events WHERE user_id = ? AND type = ? "
                "ORDER BY occurred_at DESC LIMIT ?",
                (user_id, type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM growth_events WHERE user_id = ? "
                "ORDER BY occurred_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [self._growth_row(r) for r in rows]

    def list_growth_events_range(self, user_id: int, from_ts: Optional[float] = None,
                                 to_ts: Optional[float] = None,
                                 limit: int = 200) -> list[dict[str, Any]]:
        conn = _get_conn(self.db_path)
        clauses = ["user_id = ?"]
        params: list[Any] = [user_id]
        if from_ts is not None:
            clauses.append("occurred_at >= ?")
            params.append(from_ts)
        if to_ts is not None:
            clauses.append("occurred_at <= ?")
            params.append(to_ts)
        params.append(min(max(limit, 1), 500))
        rows = conn.execute(
            f"SELECT * FROM growth_events WHERE {' AND '.join(clauses)} "
            "ORDER BY occurred_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._growth_row(r) for r in rows]

    @staticmethod
    def _growth_row(row: sqlite3.Row) -> dict[str, Any]:
        # camelCase，与契约 §2.8 GrowthEvent JSON 逐字段对齐；
        # type 原样输出 Dart enum 名，绝不翻中文（前端 tryParse 会解析失败）。
        return {
            "id": row["id"],
            "type": row["type"],
            "title": row["title"],
            "description": row["description"],
            "delta": row["delta"],
            "occurredAt": iso(row["occurred_at"]),
        }

    # ── helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_ts(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
