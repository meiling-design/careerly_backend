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

import functools
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from app.careerly import growth
from app.careerly.schemas import (
    AnalysisProject,
    AnalysisResult,
    GapDetail,
    JobMatch,
    JobModel,
    JobSnapshot,
    QuotaState,
    StrengthScore,
    iso,
)
from app.utils.logger import logger
from app.utils.paths import APP_DATA_DIR as DATA_DIR  # 可被 DATA_DIR 重定向

DB_PATH = DATA_DIR / "careerly.db"

_local = threading.local()

#: 只保护「建立连接 + 设 PRAGMA」这一步。
#:
#: ``PRAGMA journal_mode=WAL`` 本身需要一次短暂的写锁；多个线程同时
#: 首次建连接时会在这里互相撞上 ``database is locked``（P1-03 的并发
#: 测试第一次跑就暴露了）。真正的事务型 UPDATE 仍由 SQLite 自己串行化，
#: 这个锁不会把并发预占变成串行业务逻辑。
_conn_lock = threading.Lock()


def _get_conn(db_path: str = str(DB_PATH)) -> sqlite3.Connection:
    if not hasattr(_local, "careerly_conn") or _local.careerly_conn is None:
        with _conn_lock:
            if not hasattr(_local, "careerly_conn") or _local.careerly_conn is None:
                conn = sqlite3.connect(
                    db_path, timeout=30.0, check_same_thread=False,
                )
                conn.row_factory = sqlite3.Row
                # WAL 是**数据库级**持久属性，只在初始化时设置一次
                # （见 ``_init_schema``）。每条新连接再设一遍需要短暂的
                # 写锁，并发首次建连会互相等待数秒。
                conn.execute("PRAGMA busy_timeout=30000")
                _local.careerly_conn = conn
    return _local.careerly_conn


def _quota_transaction(fn):
    """串行化同一 Store 实例内的额度事务（见 ``__init__`` 的说明）。"""

    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._quota_lock:
            return fn(self, *args, **kwargs)

    return wrapper


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
        # 进程内额度事务锁：SQLite 的条件 UPDATE 已保证跨进程正确性；
        # 这把锁只是避免同进程并发请求在 SQLite busy handler 里空转数秒。
        self._quota_lock = threading.Lock()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Schema ──────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        conn = _get_conn(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
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
                resume_version_label TEXT,
                capability_snapshot TEXT,
                scoring_version     TEXT,
                resume_fingerprint  TEXT,
                created_at          REAL NOT NULL
            );

            -- P1-02：分析记录实际使用的 JD 原文快照。
            -- 与 jobs 表**分开**：jobs 是可变的岗位实体，快照是某一次分析
            -- 的只读证据。写入后永不更新；重新解析链接 / 修改岗位信息
            -- 都不得覆盖它。历史行没有记录 → 前端展示「原文缺失」，
            -- 绝不用当前 jobs.raw_jd 冒充。
            CREATE TABLE IF NOT EXISTS analysis_job_snapshots (
                id            TEXT PRIMARY KEY,
                result_id     TEXT NOT NULL,
                job_id        TEXT NOT NULL,
                title         TEXT NOT NULL DEFAULT '',
                company       TEXT NOT NULL DEFAULT '',
                raw_jd        TEXT NOT NULL DEFAULT '',
                source        TEXT,
                source_url    TEXT,
                import_method TEXT,
                captured_at   REAL NOT NULL,
                created_at    REAL NOT NULL,
                UNIQUE (result_id, job_id)
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

            -- 「当前简历」——**只有一份**，是分析链路读的那份基线。
            -- ⚠️ 它同时是 careerly_resume_versions 里 is_current=1 那条的
            -- 冗余副本，写入方必须两边一起改（见 save_resume / add_resume_version）。
            -- 之所以不取消这张表：POST /analysis 直接读它，
            -- 保留就能让分析链路一行都不用改。
            CREATE TABLE IF NOT EXISTS careerly_resumes (
                user_id    INTEGER PRIMARY KEY,
                text       TEXT NOT NULL DEFAULT '',
                profile    TEXT,
                updated_at REAL NOT NULL
            );

            -- 简历版本 —— PRD §14 / Figma `ResumeHistoryPage`：
            -- 「支持当前简历 / 历史简历 / 上传新简历 / 删除 / 设置当前简历」。
            --
            -- ⚠️ 每次 `POST /resume` 是**追加**一行，不是覆盖 —— 这正是
            -- 「上传新简历后历史列表里多出一张卡」的数据来源。
            -- 覆盖式写入（旧实现）会让「多版本」在数据层根本不可能存在。
            --
            -- `seq` 用来派生 label（`V{seq}`），取值 = 该用户当前存活的
            -- **最大序号 + 1**。
            --
            -- 为什么存列、不按行序现算：**已存在的版本号永远不变**。
            -- 按行序现算的话，删掉 V2 会让 V3 突然显示成 V2 ——
            -- 用户看到的版本号会自己跳，Figma 的 onDelete 也只是过滤列表，
            -- 不会重排剩下的版本号。
            --
            -- ⚠️ 已知的、**可达的**边界：删掉序号最大的那个版本（先把某个
            -- 小号版本「设为当前」，最大的就成了可删的归档版本）之后，
            -- 下一次上传会复用那个号 —— 列表本身仍然自洽（旧的那条已经没了），
            -- 只是同一版号在不同时间指过不同的简历。
            -- 要彻底消除，得引入永不回退的用户级计数器或软删除；
            -- 两者都会让「读列表」多一个必须记住的过滤条件，代价大于收益。
            CREATE TABLE IF NOT EXISTS careerly_resume_versions (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                seq        INTEGER NOT NULL,
                text       TEXT NOT NULL DEFAULT '',
                profile    TEXT,
                file_name  TEXT,
                original_pdf BLOB,
                created_at REAL NOT NULL,
                is_current INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_careerly_resume_versions_user
                ON careerly_resume_versions (user_id, seq DESC);

            CREATE TABLE IF NOT EXISTS user_quota (
                user_id   INTEGER PRIMARY KEY,
                total     INTEGER NOT NULL DEFAULT 3,
                used      INTEGER NOT NULL DEFAULT 0,   -- 已确认消耗
                reserved  INTEGER NOT NULL DEFAULT 0,   -- 已预占、尚未确认（P1-03）
                is_member INTEGER NOT NULL DEFAULT 0
            );

            -- AI 对话额度 —— **独立于** user_quota（裁决 2026-10-03 ②）。
            -- §5.2 的「免费 3 次完整分析」一字不动；AI 单独计数。
            -- 与 user_quota 分开建表而不是加列，是为了让两个池的
            -- 「总量 / 是否会员豁免」能各自演进，不互相牵连。
            CREATE TABLE IF NOT EXISTS user_ai_quota (
                user_id   INTEGER PRIMARY KEY,
                total     INTEGER NOT NULL DEFAULT 50,
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
                status        TEXT NOT NULL,      -- queued|processing|completed|failed|cancelled
                title         TEXT NOT NULL DEFAULT '',
                source        TEXT NOT NULL DEFAULT 'paste',
                session_id    TEXT,               -- P1-1b：引擎 session 的显式绑定
                resume_version_id    TEXT,        -- P1-01：任务创建时绑定的简历版本
                resume_version_label TEXT,
                jd_snapshot   TEXT,               -- P1-02：任务创建时的 JD 快照 JSON
                quota_state   TEXT NOT NULL DEFAULT 'none',
                                 -- none|reserved|consumed|released|member (P1-03)
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
            CREATE INDEX IF NOT EXISTS idx_job_snapshots_result
                ON analysis_job_snapshots (result_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_user ON analysis_tasks(user_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON analysis_tasks(status);
            CREATE INDEX IF NOT EXISTS idx_snapshots_user ON capability_snapshots(user_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_growth_user ON growth_events(user_id, occurred_at);
        """)
        # ── 加列迁移 ────────────────────────────────────────────────────
        # ``CREATE TABLE IF NOT EXISTS`` 对**已存在**的表是空操作，
        # 所以新增列必须在这里显式 ALTER，否则老库升级后读列就报错。
        versions_columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(careerly_resume_versions)"
            ).fetchall()
        }
        if "original_pdf" not in versions_columns:
            conn.execute(
                "ALTER TABLE careerly_resume_versions ADD COLUMN original_pdf BLOB"
            )
        task_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(analysis_tasks)").fetchall()
        }
        if "session_id" not in task_columns:
            # P1-1b：既有库补列。历史任务该列为 NULL —— 它们的引擎 session
            # 从未被绑定，进度不可回溯（迁移前本就没有可观测面可言）。
            conn.execute("ALTER TABLE analysis_tasks ADD COLUMN session_id TEXT")
        # P1-01 / P1-02 / P1-03：既有库补列。历史任务这些列为 NULL / none，
        # 表示「迁移前的数据没有这些证据」—— 不猜测、不回填。
        for column, ddl in (
            ("resume_version_id", "TEXT"),
            ("resume_version_label", "TEXT"),
            ("jd_snapshot", "TEXT"),
            ("quota_state", "TEXT NOT NULL DEFAULT 'none'"),
        ):
            if column not in task_columns:
                conn.execute(f"ALTER TABLE analysis_tasks ADD COLUMN {column} {ddl}")

        result_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(analysis_results)").fetchall()
        }
        if "resume_version_label" not in result_columns:
            conn.execute(
                "ALTER TABLE analysis_results ADD COLUMN resume_version_label TEXT"
            )
        # P1-01：比较口径随结果一起存。旧行保持 NULL → 不可比较，
        # 不补造「上次分数」（与 growth 模块的历史快照规则一致）。
        for column in ("scoring_version", "resume_fingerprint"):
            if column not in result_columns:
                conn.execute(f"ALTER TABLE analysis_results ADD COLUMN {column} TEXT")

        quota_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(user_quota)").fetchall()
        }
        if "reserved" not in quota_columns:
            conn.execute(
                "ALTER TABLE user_quota ADD COLUMN reserved INTEGER NOT NULL DEFAULT 0"
            )
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

        # project_jobs 是「本次项目当前引用哪些岗位」，不是岗位实体删除。
        # 更新后移除一个岗位只移除它的引用；jobs / 历史快照 / 匹配仍永久保留。
        conn.execute(
            "DELETE FROM project_jobs WHERE project_id = ?",
            (project.id,),
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
                "(id, project_id, version, resume_version_id, resume_version_label, "
                " capability_snapshot, scoring_version, resume_fingerprint, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (result.id, result.projectId or project.id, result.version,
                 result.resumeVersionId, result.resumeVersionLabel,
                 _j(result.capabilitySnapshot),
                 result.scoringVersion, result.resumeFingerprint,
                 self._parse_ts(result.createdAt) or now),
            )
            # P1-02：写入本次分析的 JD 快照。``INSERT OR IGNORE`` 而不是
            # REPLACE —— 同一条分析记录的快照**第一次写入即定稿**，
            # 任何后续保存都不得覆盖它。
            for snap in result.jobSnapshots:
                if not snap.available:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO analysis_job_snapshots "
                    "(id, result_id, job_id, title, company, raw_jd, source, "
                    " source_url, import_method, captured_at, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        f"jsnap_{uuid.uuid4().hex[:16]}", result.id, snap.jobId,
                        snap.title, snap.company, snap.rawJd, snap.source,
                        snap.sourceUrl, snap.importMethod,
                        self._parse_ts(snap.capturedAt) or now, now,
                    ),
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

    def get_resume_version(self, user_id: int, version_id: str) -> dict[str, Any] | None:
        """读取属于当前用户的指定简历版本。"""
        self._ensure_resume_versions(user_id)
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM careerly_resume_versions "
            "WHERE user_id = ? AND id = ?",
            (user_id, version_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "label": f"V{row['seq']}",
            "text": row["text"],
            "profile": _unjson(row["profile"], {}),
        }

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
            snapshots: list[JobSnapshot] = []
            known_job_ids: set[str] = set()
            for s in conn.execute(
                "SELECT * FROM analysis_job_snapshots WHERE result_id = ?", (r["id"],)
            ).fetchall():
                known_job_ids.add(s["job_id"])
                snapshots.append(JobSnapshot(
                    jobId=s["job_id"],
                    title=s["title"],
                    company=s["company"],
                    rawJd=s["raw_jd"],
                    source=s["source"],
                    sourceUrl=s["source_url"],
                    importMethod=s["import_method"],
                    capturedAt=iso(s["captured_at"]),
                    available=True,
                ))
            # 历史数据兼容（P1-02）：迁移前的结果没有快照行。**明确**给出
            # ``available=False``，而不是让调用方去读当前 ``jobs.raw_jd``。
            for match in matches:
                if match.jobId not in known_job_ids:
                    snapshots.append(JobSnapshot(
                        jobId=match.jobId, available=False,
                    ))
            results.append(AnalysisResult(
                id=r["id"], projectId=r["project_id"], version=r["version"],
                resumeVersionId=r["resume_version_id"],
                resumeVersionLabel=r["resume_version_label"],
                matches=matches,
                capabilitySnapshot=_unjson(r["capability_snapshot"], {}),
                jobSnapshots=snapshots,
                scoringVersion=r["scoring_version"],
                resumeFingerprint=r["resume_fingerprint"],
                createdAt=iso(r["created_at"]),
            ))

        # P1-01：匹配度变化只由后端判定。这里统一按「同一岗位 + 同一 JD +
        # 不同简历 + 同一评分口径」回填 ``JobMatch.previousScore``，前端
        # 不再拿相邻结果的 jobId 自行相减。
        growth.annotate_match_previous_scores(results)

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

    # ── Resume: current baseline + versions ─────────────────────────────
    #
    # 两张表的关系（写这段前先读一遍，否则很容易只改一边）：
    #
    #   careerly_resumes          当前简历，**一份**。分析链路（POST /analysis）
    #                             直接读它，所以它必须永远等于「当前版本」。
    #   careerly_resume_versions  全部版本，**每次上传追加一行**。
    #
    # 不变式：is_current=1 的那一行，其 text / profile 与 careerly_resumes 相同。
    # 下面每个写方法都同时维护两边 —— 这是**故意**的冗余：
    # 换来的是一行都不改的分析链路。

    def save_resume(self, user_id: int, text: str, profile: dict | None = None) -> None:
        """刷新**当前版本**的文本 / 画像 —— 不产生新版本。

        调用方是 ``POST /analysis``（分析链路顺带重解析）与 ``PUT /resume``
        （只改基线文本）。两者语义都是「把当前这份简历更新一下」，
        而不是「用户又交了一份新简历」—— 后者走 [add_resume_version]。

        ⚠️ 若该用户此前只有旧的单基线数据，这里会顺带把版本表回填出来，
        否则「当前版本」会变成一条查不到的孤儿记录。
        """
        conn = _get_conn(self.db_path)
        now = time.time()
        conn.execute(
            "INSERT OR REPLACE INTO careerly_resumes (user_id, text, profile, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, text, _j(profile or {}), now),
        )
        self._ensure_resume_versions(user_id)
        conn.execute(
            "UPDATE careerly_resume_versions SET text = ?, profile = ? "
            "WHERE user_id = ? AND is_current = 1",
            (text, _j(profile or {}), user_id),
        )
        conn.commit()

    def update_resume_version_profile(
        self, user_id: int, version_id: str, profile: dict
    ) -> None:
        """把分析解析出的画像写回**它实际使用的那一版**简历（P1-01）。

        为什么不是 ``save_resume``：分析可能跑了十几分钟，期间用户完全
        可能把当前简历切成另一版。``save_resume`` 写的是「当前版本」，
        那会把旧简历的文本 / 画像覆盖到新版本上。这里按 ``version_id``
        精确定位；只有当它**仍然是当前版本**时才同步冗余的基线表。
        """
        conn = _get_conn(self.db_path)
        cur = conn.execute(
            "UPDATE careerly_resume_versions SET profile = ? "
            "WHERE user_id = ? AND id = ?",
            (_j(profile), user_id, version_id),
        )
        if cur.rowcount == 0:
            conn.commit()
            return
        conn.execute(
            "UPDATE careerly_resumes SET profile = ? WHERE user_id = ? "
            "AND EXISTS (SELECT 1 FROM careerly_resume_versions "
            "            WHERE user_id = ? AND id = ? AND is_current = 1)",
            (_j(profile), user_id, user_id, version_id),
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

    # ── Resume versions ─────────────────────────────────────────────────

    def _ensure_resume_versions(self, user_id: int) -> None:
        """把「只有单基线」的历史用户回填成第一条版本记录。

        幂等：版本表里已经有该用户的行就什么也不做。
        没有这一步，多版本功能上线前注册的用户打开「我的简历」
        会看到空列表 —— 而他们的简历明明还在 careerly_resumes 里。
        """
        conn = _get_conn(self.db_path)
        exists = conn.execute(
            "SELECT 1 FROM careerly_resume_versions WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
        if exists is not None:
            return
        base = conn.execute(
            "SELECT * FROM careerly_resumes WHERE user_id = ?", (user_id,)
        ).fetchone()
        if base is None:
            return
        conn.execute(
            "INSERT INTO careerly_resume_versions "
            "(id, user_id, seq, text, profile, file_name, created_at, is_current) "
            "VALUES (?, ?, 1, ?, ?, NULL, ?, 1)",
            (uuid.uuid4().hex, user_id, base["text"],
             base["profile"] or "{}", base["updated_at"]),
        )
        conn.commit()

    @staticmethod
    def _version_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            # label 由 seq 派生而不是存列 —— 只有一处能产生版号，
            # 不会出现「列里的 label 和 seq 对不上」这种漂移。
            "label": f"V{row['seq']}",
            "fileName": row["file_name"],
            "uploadedAt": iso(row["created_at"]),
            "isCurrent": bool(row["is_current"]),
        }

    def get_current_resume_version(self, user_id: int) -> dict[str, Any] | None:
        """当前简历版本 ``{id, label, text, profile}``，没有基线时 ``None``。

        P1-01：分析任务创建时调用一次，把「本次实际使用的简历版本」
        固定下来。之后用户切到别的版本，已有分析记录的绑定不受影响。
        """
        self._ensure_resume_versions(user_id)
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM careerly_resume_versions "
            "WHERE user_id = ? AND is_current = 1",
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "label": f"V{row['seq']}",
            "text": row["text"],
            "profile": _unjson(row["profile"], {}),
            "createdAt": iso(row["created_at"]),
        }

    def list_resume_versions(self, user_id: int) -> list[dict[str, Any]]:
        """全部版本，当前版本排第一，其余按版号倒序（新的在前）。"""
        self._ensure_resume_versions(user_id)
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT * FROM careerly_resume_versions WHERE user_id = ? "
            "ORDER BY is_current DESC, seq DESC",
            (user_id,),
        ).fetchall()
        return [self._version_row(r) for r in rows]

    def add_resume_version(
        self,
        user_id: int,
        text: str,
        profile: dict | None = None,
        file_name: str | None = None,
        original_pdf: bytes | None = None,
    ) -> dict[str, Any]:
        """追加一份新简历版本，并把它设为当前简历。

        这是 `POST /resume` 的落库路径 —— 「上传新简历」在数据层
        就是**多一行**，历史版本列表随之多出一张卡。
        """
        self._ensure_resume_versions(user_id)
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM careerly_resume_versions "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        seq = int(row["m"]) + 1
        version_id = uuid.uuid4().hex
        now = time.time()

        conn.execute(
            "UPDATE careerly_resume_versions SET is_current = 0 WHERE user_id = ?",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO careerly_resume_versions "
            "(id, user_id, seq, text, profile, file_name, original_pdf, "
            "created_at, is_current) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                version_id, user_id, seq, text, _j(profile or {}),
                file_name, original_pdf, now,
            ),
        )
        # 同步「当前简历」基线 —— 不变式见本区块顶部说明。
        conn.execute(
            "INSERT OR REPLACE INTO careerly_resumes (user_id, text, profile, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, text, _j(profile or {}), now),
        )
        conn.commit()
        return next(
            v for v in self.list_resume_versions(user_id) if v["id"] == version_id
        )

    def get_resume_version_pdf(
        self, user_id: int, version_id: str
    ) -> tuple[str, bytes] | None:
        """只读取当前用户某个版本保存的原始 PDF。"""
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT file_name, original_pdf FROM careerly_resume_versions "
            "WHERE user_id = ? AND id = ?",
            (user_id, version_id),
        ).fetchone()
        if row is None or row["original_pdf"] is None:
            return None
        return row["file_name"] or "resume.pdf", bytes(row["original_pdf"])

    def set_current_resume_version(self, user_id: int, version_id: str) -> bool:
        """把某个历史版本提为当前简历。找不到该版本时返回 `False`。"""
        self._ensure_resume_versions(user_id)
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM careerly_resume_versions WHERE user_id = ? AND id = ?",
            (user_id, version_id),
        ).fetchone()
        if row is None:
            return False

        conn.execute(
            "UPDATE careerly_resume_versions SET is_current = 0 WHERE user_id = ?",
            (user_id,),
        )
        conn.execute(
            "UPDATE careerly_resume_versions SET is_current = 1 WHERE id = ?",
            (version_id,),
        )
        # `updated_at` 取该版本自己的创建时间，**不是** now()。
        # 「更新于」说的是「这份简历是什么时候的」，把切换动作的时刻写进去
        # 会让一份 5 月的简历显示成今天更新的。
        conn.execute(
            "INSERT OR REPLACE INTO careerly_resumes (user_id, text, profile, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, row["text"], row["profile"] or "{}", row["created_at"]),
        )
        conn.commit()
        return True

    def delete_resume_version(self, user_id: int, version_id: str) -> str:
        """删除一个历史版本。

        返回 ``"ok"`` / ``"not_found"`` / ``"current"`` —— 用字符串而不是
        抛异常，是因为这三种结果需要映射成**不同**的 HTTP 语义
        （204 / 404 / 409），异常类型在这里反而不好分。

        ⚠️ **当前简历不可删**。Figma 的删除按钮只出现在历史版本卡上
        （`SwipeableResumeCard` 一律带「归档」胶囊），当前简历一旦删掉，
        `careerly_resumes` 就没有来源了 —— 分析链路会直接断。
        """
        self._ensure_resume_versions(user_id)
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT is_current FROM careerly_resume_versions "
            "WHERE user_id = ? AND id = ?",
            (user_id, version_id),
        ).fetchone()
        if row is None:
            return "not_found"
        if row["is_current"]:
            return "current"

        conn.execute(
            "DELETE FROM careerly_resume_versions WHERE user_id = ? AND id = ?",
            (user_id, version_id),
        )
        conn.commit()
        return "ok"

    # ── Quota ───────────────────────────────────────────────────────────

    FREE_ANALYSIS_QUOTA = 3  # Baseline §5 / PRD §16

    def get_quota(self, user_id: int) -> QuotaState:
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM user_quota WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return QuotaState(total=self.FREE_ANALYSIS_QUOTA, used=0, reserved=0,
                              isMember=False)
        return QuotaState(
            total=row["total"], used=row["used"], reserved=row["reserved"],
            isMember=bool(row["is_member"]),
        )

    #: 预占结果取值 —— 调用方据此决定是否放行任务。
    QUOTA_RESERVED = "reserved"
    QUOTA_MEMBER = "member"
    QUOTA_EXHAUSTED = "exhausted"

    @_quota_transaction
    def reserve_analysis_quota(self, user_id: int, task_id: str) -> str:
        """原子预占一次分析额度（P1-03）。

        额度检查与扣减曾经是两步：并发请求可以同时通过 ``used < total``
        的检查，然后都成功，最终 ``used`` 超过 ``total``。这里把
        「检查 + 预占」压缩成**一条带条件的 UPDATE** —— SQLite 保证它
        原子执行，并发的第二个请求会看到 ``reserved`` 已加 1，条件
        不再成立。

        返回 ``reserved`` / ``member`` / ``exhausted``。会员不预占
        （额度无限），但仍记录 ``quota_state='member'`` 以便恢复逻辑
        知道「这个任务没有占用免费额度」。
        """
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT is_member FROM user_quota WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            # 只在首次时写入；并发首写由 OR IGNORE 兜底。
            conn.execute(
                "INSERT OR IGNORE INTO user_quota "
                "(user_id, total, used, reserved, is_member) VALUES (?, ?, 0, 0, 0)",
                (user_id, self.FREE_ANALYSIS_QUOTA),
            )
            row = conn.execute(
                "SELECT is_member FROM user_quota WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is not None and row["is_member"]:
            conn.execute(
                "UPDATE analysis_tasks SET quota_state = 'member' WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            )
            conn.commit()
            return self.QUOTA_MEMBER

        cur = conn.execute(
            "UPDATE user_quota SET reserved = reserved + 1 "
            "WHERE user_id = ? AND is_member = 0 AND used + reserved < total",
            (user_id,),
        )
        if cur.rowcount == 0:
            conn.commit()
            return self.QUOTA_EXHAUSTED

        conn.execute(
            "UPDATE analysis_tasks SET quota_state = 'reserved' WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        conn.commit()
        return self.QUOTA_RESERVED

    @_quota_transaction
    def commit_analysis_quota(self, task_id: str) -> bool:
        """把任务的预占确认为已消耗（幂等）。

        ``quota_state='reserved'`` → ``'consumed'`` 的条件更新是第一道
        幂等闸：重复确认、任务已取消 / 已失败时都不会再动额度。
        """
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT user_id, quota_state FROM analysis_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["quota_state"] != "reserved":
            return False
        cur = conn.execute(
            "UPDATE analysis_tasks SET quota_state = 'consumed' "
            "WHERE id = ? AND quota_state = 'reserved'",
            (task_id,),
        )
        if cur.rowcount == 0:
            conn.commit()
            return False
        conn.execute(
            "UPDATE user_quota SET "
            "  reserved = CASE WHEN reserved > 0 THEN reserved - 1 ELSE 0 END, "
            "  used = used + 1 "
            "WHERE user_id = ? AND is_member = 0",
            (row["user_id"],),
        )
        conn.commit()
        return True

    @_quota_transaction
    def release_analysis_quota(self, task_id: str) -> bool:
        """释放任务的预占（幂等）。失败 / 取消 / 重启恢复都走这里。"""
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT user_id, quota_state FROM analysis_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["quota_state"] != "reserved":
            return False
        cur = conn.execute(
            "UPDATE analysis_tasks SET quota_state = 'released' "
            "WHERE id = ? AND quota_state = 'reserved'",
            (task_id,),
        )
        if cur.rowcount == 0:
            conn.commit()
            return False
        conn.execute(
            "UPDATE user_quota SET "
            "  reserved = CASE WHEN reserved > 0 THEN reserved - 1 ELSE 0 END "
            "WHERE user_id = ? AND is_member = 0",
            (row["user_id"],),
        )
        conn.commit()
        return True

    def consume_quota(self, user_id: int) -> None:
        """Deduct one analysis directly (legacy helper).

        ⚠️ 新代码不要用这个 —— 它绕过预占，正是 P1-03 的并发竞争来源。
        分析链路走 :meth:`reserve_analysis_quota` + :meth:`commit_analysis_quota`。
        保留是为了兼容仍按「成功即扣一次」语义调用的旧测试 / 脚本。
        """
        conn = _get_conn(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO user_quota (user_id, total, used, reserved, is_member) "
            "VALUES (?, ?, 0, 0, 0)",
            (user_id, self.FREE_ANALYSIS_QUOTA),
        )
        conn.execute(
            "UPDATE user_quota SET used = used + 1 WHERE user_id = ? AND is_member = 0",
            (user_id,),
        )
        conn.commit()

    # ── AI 对话额度（独立池，裁决 2026-10-03 ②）──────────────────────────
    #
    # ⚠️ 暂定值 · 待产品裁决
    # 裁决只定了「AI 额度**独立于**分析额度、§5.2 不动」，
    # **没有**定具体免费次数。这个数字只用于让「检查 / 扣减」链路
    # 真实可跑，**不是产品规则**；产品侧定了数字后改这一处即可
    # （表默认值同步改 `_init_schema`）。

    FREE_AI_QUOTA = 50

    def get_ai_quota(self, user_id: int) -> QuotaState:
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT * FROM user_ai_quota WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return QuotaState(total=self.FREE_AI_QUOTA, used=0, isMember=False)
        return QuotaState(
            total=row["total"], used=row["used"], isMember=bool(row["is_member"])
        )

    def consume_ai_quota(self, user_id: int) -> None:
        """Deduct one AI message. Only call AFTER the answer streamed cleanly.

        ⚠️ **已知并发风险，本批次不修（见 docs/careerly/P1_FOLLOWUPS.md F1）**：
        AI 额度仍是「先检查、流结束后扣减」，与 P1-04 修复前的分析额度同构。
        同一用户并发发起多条对话可能同时通过检查。用户明确要求 AI 额度
        不纳入本批次重构，故只记录、不顺手扩大修改范围。

        与 `consume_quota` 同一纪律：**先检查、后扣减、只有成功才扣**。
        失败（含流中断）不扣 —— 用户没有拿到回答，不该付这次数。
        """
        conn = _get_conn(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO user_ai_quota (user_id, total, used, is_member) "
            "VALUES (?, ?, 0, 0)",
            (user_id, self.FREE_AI_QUOTA),
        )
        conn.execute(
            "UPDATE user_ai_quota SET used = used + 1 WHERE user_id = ? AND is_member = 0",
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
    TASK_CANCELLED = "cancelled"

    #: 终态集合：轮询循环 / 取消接口据此判断「还能不能取消」。
    TERMINAL_TASK_STATUSES = frozenset(
        {TASK_COMPLETED, TASK_FAILED, TASK_CANCELLED}
    )

    def create_task(
        self,
        user_id: int,
        title: str,
        source: str = "paste",
        session_id: str | None = None,
        resume_version_id: str | None = None,
        resume_version_label: str | None = None,
        jd_snapshot: Any = None,
    ) -> str:
        """Insert a ``queued`` task and return its id.

        ``session_id``（P1-1b）—— 预先绑定本次分析将要使用的引擎 session。
        由 **API 层生成**后同时交给引擎，于是任务在 ``queued``/``processing``
        期间就能通过 ``GET /analysis/tasks/{id}`` 报告真实节点进度。
        不传则为 NULL：任务与引擎轨迹无关联，只有终态可观测。

        ``resume_version_id`` / ``jd_snapshot``（P1-01 / P1-02）——
        **在任务创建时**就固定住「这次分析实际用了哪份简历、哪份 JD 原文」。
        成功落库时写进 ``analysis_results`` / ``analysis_job_snapshots``；
        失败也留在任务行里，作为可追溯的运行证据。
        """
        task_id = f"task_{uuid.uuid4().hex[:16]}"
        conn = _get_conn(self.db_path)
        conn.execute(
            "INSERT INTO analysis_tasks (id, user_id, project_id, status, title, source, "
            " session_id, resume_version_id, resume_version_label, jd_snapshot, "
            " quota_state, error_code, error_message, created_at, started_at, finished_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 'none', NULL, NULL, ?, NULL, NULL)",
            (
                task_id, user_id, self.TASK_QUEUED, title.strip(), source,
                session_id, resume_version_id, resume_version_label,
                _j(jd_snapshot) if jd_snapshot else None,
                time.time(),
            ),
        )
        conn.commit()
        return task_id

    def get_task_jd_snapshot(self, task_id: str) -> dict[str, Any] | None:
        conn = _get_conn(self.db_path)
        row = conn.execute(
            "SELECT jd_snapshot FROM analysis_tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None:
            return None
        return _unjson(row["jd_snapshot"], None)

    def start_task(self, task_id: str) -> bool:
        """queued → processing。任务已被取消时返回 ``False``，调用方不得再跑引擎。"""
        conn = _get_conn(self.db_path)
        cur = conn.execute(
            "UPDATE analysis_tasks SET status = ?, started_at = ? "
            "WHERE id = ? AND status = ?",
            (self.TASK_PROCESSING, time.time(), task_id, self.TASK_QUEUED),
        )
        conn.commit()
        return cur.rowcount > 0

    def complete_task(self, task_id: str, project_id: str) -> bool:
        """Mark a task completed **and** confirm its reserved quota.

        只有 ``queued`` / ``processing`` 的任务能被完成 —— 条件更新同时
        挡住了两种错误：已取消的任务被迟到的结果覆盖、已完成的任务被
        重复确认（P1-05 / P1-03）。
        """
        conn = _get_conn(self.db_path)
        cur = conn.execute(
            "UPDATE analysis_tasks SET status = ?, project_id = ?, finished_at = ?, "
            " error_code = NULL, error_message = NULL "
            "WHERE id = ? AND status IN (?, ?)",
            (
                self.TASK_COMPLETED, project_id, time.time(), task_id,
                self.TASK_QUEUED, self.TASK_PROCESSING,
            ),
        )
        if cur.rowcount == 0:
            conn.commit()
            return False
        conn.commit()
        self.commit_analysis_quota(task_id)
        return True

    def fail_task(self, task_id: str, error_code: str, error_message: str) -> bool:
        """Mark a task failed and release its quota reservation.

        已经到终态的任务不会被降级 —— 取消 / 成功之后迟到的失败不再改写
        状态，也不会重复释放额度（``release_analysis_quota`` 自身幂等）。
        """
        conn = _get_conn(self.db_path)
        cur = conn.execute(
            "UPDATE analysis_tasks SET status = ?, finished_at = ?, error_code = ?, "
            " error_message = ? WHERE id = ? AND status IN (?, ?)",
            (
                self.TASK_FAILED, time.time(), error_code, error_message[:2000],
                task_id, self.TASK_QUEUED, self.TASK_PROCESSING,
            ),
        )
        if cur.rowcount == 0:
            conn.commit()
            return False
        conn.commit()
        self.release_analysis_quota(task_id)
        return True

    def cancel_task(
        self,
        task_id: str,
        error_message: str = "分析已取消",
    ) -> None:
        """把未终结的任务标成 cancelled（幂等）。

        ⚠️ 只允许从 queued / processing 迁移；已经 completed / failed 的任务
        不会被降级。客户端重复点「退出」、或取消请求与任务完成并发时，
        先到者决定终态。
        """
        conn = _get_conn(self.db_path)
        cur = conn.execute(
            "UPDATE analysis_tasks SET status = ?, finished_at = ?, "
            " error_code = 'ANALYSIS_CANCELLED', error_message = ? "
            "WHERE id = ? AND status IN (?, ?)",
            (
                self.TASK_CANCELLED,
                time.time(),
                error_message[:2000],
                task_id,
                self.TASK_QUEUED,
                self.TASK_PROCESSING,
            ),
        )
        if cur.rowcount == 0:
            conn.commit()
            return
        conn.commit()
        self.release_analysis_quota(task_id)

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

    def recover_interrupted_tasks(self) -> int:
        """启动时的任务恢复（P1-06）。

        ``asyncio.create_task`` 创建的协程无法跨进程存活。进程重启后，
        数据库里可能残留 ``queued`` / ``processing`` 的任务 —— 它们**不可能**
        再被任何协程推进。这里：

        1. 把这些任务标成 ``failed`` + ``TASK_INTERRUPTED``；
        2. 释放它们尚未确认的额度预占；
        3. **不重新执行**分析（没有可靠的恢复依据，重跑可能重复扣费）。

        同时修复两类「状态已终态但预占未结清」的遗留行：
        ``completed + reserved`` → 确认消耗；``failed/cancelled + reserved``
        → 释放。这样进程在「写项目」与「确认额度」之间被杀也不会留下
        永久占用的额度。

        返回被标记为中断的任务数（便于启动日志自检）。
        """
        conn = _get_conn(self.db_path)
        rows = conn.execute(
            "SELECT id, status, quota_state FROM analysis_tasks "
            "WHERE status IN (?, ?)",
            (self.TASK_QUEUED, self.TASK_PROCESSING),
        ).fetchall()

        interrupted = 0
        for row in rows:
            if self.fail_task(
                row["id"],
                "TASK_INTERRUPTED",
                "服务重启导致分析任务中断，请重新发起分析",
            ):
                interrupted += 1

        # 终态任务与预占状态不一致的遗留行（见 docstring）。
        terminal = conn.execute(
            "SELECT id, status, quota_state FROM analysis_tasks "
            "WHERE status IN (?, ?, ?) AND quota_state = 'reserved'",
            (self.TASK_COMPLETED, self.TASK_FAILED, self.TASK_CANCELLED),
        ).fetchall()
        for row in terminal:
            if row["status"] == self.TASK_COMPLETED:
                self.commit_analysis_quota(row["id"])
            else:
                self.release_analysis_quota(row["id"])

        if interrupted:
            logger.info(
                "CareerlyStore: recovered %d interrupted analysis task(s)",
                interrupted,
            )
        return interrupted

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
            "sessionId": row["session_id"],
            "title": row["title"],
            "source": row["source"],
            "resumeVersionId": row["resume_version_id"],
            "resumeVersionLabel": row["resume_version_label"],
            "quotaState": row["quota_state"],
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
        return self._snapshot_row(row)

    def get_previous_snapshot(self, user_id: int, snapshot_id: str) -> Optional[dict[str, Any]]:
        """The snapshot immediately **before** ``snapshot_id``, or None.

        P3-1：``CareerAssets.capabilities`` 需要 ``previousScore`` 才能算出
        ``delta``。按「比目标快照更早」而不是「第二新」来取 —— 两者在只有
        两条快照时结果相同，但前者在用户并发分析 / 补写历史时不会取错基准。

        ⚠️ 排序键必须与 :meth:`get_latest_snapshot` 完全一致
        （``created_at DESC, id DESC``），否则「上一条」在时间戳相同时会漂移，
        导致同一个 ``delta`` 两次请求算出不同结果。
        """
        conn = _get_conn(self.db_path)
        target = conn.execute(
            "SELECT created_at FROM capability_snapshots WHERE id = ? AND user_id = ?",
            (snapshot_id, user_id),
        ).fetchone()
        if target is None:
            return None
        row = conn.execute(
            "SELECT * FROM capability_snapshots WHERE user_id = ? "
            "  AND (created_at < ? OR (created_at = ? AND id < ?)) "
            "ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id, target["created_at"], target["created_at"], snapshot_id),
        ).fetchone()
        if row is None:
            return None
        return self._snapshot_row(row)

    def get_previous_compatible_snapshot(
        self, user_id: int, snapshot_id: str, scoring_version: str | None
    ) -> Optional[dict[str, Any]]:
        """上一条**评分口径兼容**的快照（P1-03），没有则 ``None``。

        能力分只能在相同 ``scoringVersion`` 之间比较。旧快照没有版本号
        （功能上线前写入）→ 不可比较，返回 ``None``；前端显示 ``delta=0``，
        而不是拿两个不同口径的数字硬算差值。
        """
        if not scoring_version:
            return None
        conn = _get_conn(self.db_path)
        target = conn.execute(
            "SELECT created_at FROM capability_snapshots WHERE id = ? AND user_id = ?",
            (snapshot_id, user_id),
        ).fetchone()
        if target is None:
            return None
        rows = conn.execute(
            "SELECT * FROM capability_snapshots WHERE user_id = ? "
            "  AND (created_at < ? OR (created_at = ? AND id < ?)) "
            "ORDER BY created_at DESC, id DESC",
            (user_id, target["created_at"], target["created_at"], snapshot_id),
        ).fetchall()
        for row in rows:
            candidate = self._snapshot_row(row)
            scores = candidate.get("scores") or {}
            if scores.get("scoringVersion") == scoring_version:
                return candidate
        return None

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "source": row["source"],
            "sourceId": row["source_id"],
            "scores": _unjson(row["scores"], {}),
            "createdAt": iso(row["created_at"]),
        }

    def list_capabilities(self, user_id: int) -> list[dict[str, Any]]:
        """Current capability profile with deltas vs the previous snapshot (P3-1/2).

        Returns ``[{"name": str, "score": int, "delta": int, "previousScore": int|None}]``
        — the shape ``CareerAssets.capabilities`` needs.

        ⚠️ Lives on the store (not in ``assets.py``) because **both**
        ``career/assets`` and ``home/summary`` need it. Deriving it twice would
        let the two screens disagree about whether a score went up — exactly
        the kind of drift the diff-based event model exists to prevent.

        Returns ``[]`` when there is no snapshot, or when the newest one has an
        empty capability set (C4 degraded) — 「还没有能力画像」 is a legitimate
        state, not an error.
        """
        latest = self.get_latest_snapshot(user_id)
        if not latest:
            return []
        scores: dict[str, Any] = (latest.get("scores") or {}).get("capabilities") or {}
        if not scores:
            return []

        latest_scores = latest.get("scores") or {}
        # P1-03：只和**同一评分口径**的上一条快照比。旧快照没有
        # scoringVersion → 不再产生可能误导的 delta。
        previous = self.get_previous_compatible_snapshot(
            user_id, latest["id"], latest_scores.get("scoringVersion")
        )
        prev_scores: dict[str, Any] = (
            (previous.get("scores") or {}).get("capabilities") or {} if previous else {}
        )

        out: list[dict[str, Any]] = []
        for name, score in scores.items():
            try:
                score_int = int(score)
            except (TypeError, ValueError):
                continue
            before = prev_scores.get(name)
            try:
                before_int = int(before) if before is not None else None
            except (TypeError, ValueError):
                before_int = None
            out.append({
                "name": str(name),
                "score": score_int,
                "delta": (score_int - before_int) if before_int is not None else 0,
                "previousScore": before_int,
            })
        return out

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
