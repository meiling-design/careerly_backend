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

            CREATE INDEX IF NOT EXISTS idx_projects_user ON analysis_projects(user_id);
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_id);
            CREATE INDEX IF NOT EXISTS idx_results_project ON analysis_results(project_id);
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
