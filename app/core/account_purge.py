"""账号数据级联清除（决策 D119 / 发布基线 K19）。

一个用户的数据散落在**四个 SQLite 库 + 三类落盘文件**里，没有任何外键
把它们连起来。所以「删除账号」不是一句 ``DELETE FROM users``，而是一张
必须逐项走完的清单 —— **漏掉任何一项，用户的简历就还留在磁盘上**，
而用户已经看到「删除成功」了。

调用方
------
```text
scripts/purge_passwordless_accounts.py   清空历史无密码账号（切 formal 之前）
scripts/purge_deleted_accounts.py        30 天后物理清理已注销账号
DELETE /api/v1/account/career-data        用户主动删除职业数据，保留账号
DELETE /api/v1/account（router_account） 用户主动注销 → 只做软删除
```

为什么本模块与「软删除」分开
----------------------------
用户点「删除账号」时**只软删除**（``status='deleted'`` + 吊销令牌）：
立即生效、可审计、误删可恢复。物理级联放到 30 天后离线跑 —— 那时候
失败可以重跑，代价低得多（见 ``auth_store.soft_delete_user`` 的说明）。

dry-run 的实现方式
------------------
``dry_run=True`` **不是**另写一套 COUNT 查询，而是在事务里真正执行全部
DELETE 然后 ``ROLLBACK``。这样「预演」与「执行」用的是**同一批语句**，
不可能出现「预演说删 6 行、实际删了 7 行」这类因两套谓词不同步而生的偏差。
SQLite 支持 DDL/DML 事务回滚，这几张表都是普通表，可以这样做。

⚠️ 已知无法覆盖的部分
----------------------
``app/data/outputs/`` 下按 ``session-<hex>`` / ``resume-<hex>`` / ``match-<hex>``
命名的产物，只能经 ``session_id`` 反查用户；而实测该库 ``sessions.user_id``
**全为 NULL**，且历史 session 行已被清理 —— 3,016 个产物文件里 2,960 个
**归属不到任何用户**（其中 291 个是含姓名/邮箱/手机号/简历全文的
``resume_parsed_session-*.json``）。

本模块对此的立场是**如实报告**，不做猜测性删除：把这些文件按「旧」
一刀切删掉，会连带删掉别人的数据。详见 :attr:`PurgeReport.orphan_artifacts`。
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from app.utils import paths
from app.utils.paths import APP_DATA_DIR, ROOT_DATA_DIR

# ── 路径 ───────────────────────────────────────────────────────────────────
# 由 app.utils.paths 统一给出，不依赖 cwd（脚本可从任意目录调用），
# 且可被 DATA_DIR 重定向到数据盘。
#
# ⚠️ 两个 graph_state.db **同名不同文件**，必须分别锚定两棵树：
#   app/data/graph_state.db ← 引擎会话轨迹（APP_DATA_DIR）
#   data/graph_state.db     ← DemoGuard 试用计数（ROOT_DATA_DIR）
# 合并成一个基准会串数据。
AUTH_DB = APP_DATA_DIR / "auth.db"
CAREERLY_DB = APP_DATA_DIR / "careerly.db"
ENGINE_DB = APP_DATA_DIR / "graph_state.db"
DEMO_DB = ROOT_DATA_DIR / "graph_state.db"

# 产物目录。⚠️ 这些路径在不同调用点**并不一致**（见下方说明），
# 因此这里把「实际存在」的都列出来，逐个扫，漏一个就等于漏一份 PII。
#
# 已知的历史不一致（本次不动，另记 TODO）：
#   app/agents/*.py            → app/data/outputs   ← 实际写入的位置
#   app/core/config.py         → app/data/outputs
#   app/api/router_v1.py:760   → data/outputs       ← 不存在，读不到文件
#   app/api/server.py:395      → data/outputs       ← 同上
ARTIFACT_DIRS = (
    APP_DATA_DIR / "outputs",   # 真正有内容的目录
    ROOT_DATA_DIR / "outputs",  # 部分调用点以为的目录
    APP_DATA_DIR / "resumes",
    ROOT_DATA_DIR / "resumes",
    APP_DATA_DIR / "uploads",
    ROOT_DATA_DIR / "uploads",
)

# ``careerly.db`` 中直接带 ``user_id`` 的表（删起来没有歧义）。
_CAREERLY_DIRECT = (
    "user_quota",
    "user_ai_quota",
    "growth_events",
    "capability_snapshots",
    "analysis_tasks",
    "careerly_resume_versions",   # ⚠️ 含 original_pdf BLOB —— 最敏感的一张
    "careerly_resumes",
)

# ``graph_state.db``（引擎）中经 session_id 关联的表。顺序无所谓 ——
# 都是按 session_id 直接删，不互相依赖。
_ENGINE_BY_SESSION = ("jobs", "match_results", "progress_logs", "resume")


@dataclass
class PurgeReport:
    """一次清除的结果（``dry_run=True`` 时是**将会**删除的数量）。"""

    user_id: int
    dry_run: bool = True
    rows: dict[str, int] = field(default_factory=dict)
    files: dict[str, int] = field(default_factory=dict)
    session_ids: list[str] = field(default_factory=list)
    #: 归属不到用户的产物文件数（**不会被删除**，只报告）
    orphan_artifacts: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    @property
    def total_files(self) -> int:
        return sum(self.files.values())

    def lines(self) -> Iterable[str]:
        """给脚本打印用的人类可读摘要。"""
        mode = "预演（未改动任何数据）" if self.dry_run else "已执行"
        yield f"账号 #{self.user_id} —— {mode}"
        if not self.rows and not self.files:
            yield "  （没有找到任何关联数据）"
        for name, count in sorted(self.rows.items()):
            if count:
                yield f"  行  {name:<34} {count}"
        for name, count in sorted(self.files.items()):
            if count:
                yield f"  文件 {name:<33} {count}"
        yield f"  合计 行 {self.total_rows} ／ 文件 {self.total_files}"
        for warning in self.warnings:
            # 不用 emoji：Windows 控制台是 GBK，打印 ⚠️ 会 UnicodeEncodeError
            # 打断整个脚本（CLAUDE.md §10.3）。中文本身在 GBK 里没问题。
            yield f"  [警告] {warning}"


# ── 工具 ───────────────────────────────────────────────────────────────────


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _run(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    """执行一条写语句，返回受影响行数。"""
    return conn.execute(sql, params).rowcount


def _placeholders(values: list[Any]) -> str:
    return ",".join("?" * len(values))


def _collect_ids(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[Any]:
    return [row[0] for row in conn.execute(sql, params).fetchall()]


def _count_orphan_artifacts(session_ids: set[str]) -> int:
    """统计产物目录里**无法归属到给定 session**的文件数。

    这些文件不会被删除 —— 它们属于别的用户，或者属于无从追查的历史
    session。计数是为了让「删除不完整」这件事在报告里**可见**，
    而不是让它安静地藏在成功信息后面。

    扫的目录与 :func:`_purge_files` 完全一致（``ARTIFACT_DIRS``）——
    两处若用不同的目录集合，「删了什么」与「报告了什么」就会对不上。
    """
    total = reachable = 0
    for directory in ARTIFACT_DIRS:
        if not directory.is_dir():
            continue
        for name in os.listdir(directory):
            if name == ".gitkeep":
                continue
            total += 1
            if _belongs_to_any(name, session_ids):
                reachable += 1
    return total - reachable


def _belongs_to_any(filename: str, session_ids: set[str]) -> bool:
    """文件名里是否带着给定的 session id。

    产物命名形如 ``report_session-<hex>.json`` / ``resume_parsed_session-<hex>.json``
    —— session id 是文件名的一部分（含 ``session-`` 前缀），直接子串匹配即可。
    """
    return any(sid and sid in filename for sid in session_ids)


# ── 主流程 ─────────────────────────────────────────────────────────────────


def purge_user(user_id: int, *, dry_run: bool = True) -> PurgeReport:
    """清除 *user_id* 的职业数据，保留 ``auth.db`` 中的账号行。

    ``dry_run=True``：在事务里跑完全部 DELETE 后 ``ROLLBACK``，
    只返回「将会删除」的数量。

    ⚠️ 本函数不修改 ``auth.db`` 中的用户记录或 token 版本。账号注销的延迟
    清理流程会在调用本函数后，由脚本另行调用 ``hard_delete_user``。
    """
    report = PurgeReport(user_id=user_id, dry_run=dry_run)

    auth = _connect(AUTH_DB)
    careerly = _connect(CAREERLY_DB)
    engine = _connect(ENGINE_DB)
    demo = _connect(DEMO_DB)
    conns = [c for c in (auth, careerly, engine, demo) if c is not None]

    try:
        for conn in conns:
            conn.execute("BEGIN")

        # ── 1. 先取 session_id：它是引擎库与文件产物的唯一线索 ──────────
        if engine is not None:
            rows = engine.execute(
                "SELECT session_id FROM sessions WHERE user_id = ?", (user_id,)
            ).fetchall()
            report.session_ids = [r[0] for r in rows]
            if not report.session_ids:
                report.warnings.append(
                    "引擎库中该用户没有可归属的 session —— "
                    "outputs/ 下的产物文件将无法按用户定位"
                )
        session_ids = set(report.session_ids)

        # ── 2. careerly.db：先收集 join 表要用的 id ────────────────────
        if careerly is not None:
            project_ids = _collect_ids(
                careerly, "SELECT id FROM analysis_projects WHERE user_id = ?", (user_id,)
            )
            job_ids = _collect_ids(careerly, "SELECT id FROM jobs WHERE user_id = ?", (user_id,))
            # analysis_results 自身没有 user_id —— 只能经 project_id 反查
            result_ids = (
                _collect_ids(
                    careerly,
                    f"SELECT id FROM analysis_results WHERE project_id IN ({_placeholders(project_ids)})",
                    tuple(project_ids),
                )
                if project_ids
                else []
            )

            # join 表必须在被引用方之前删
            deleted = 0
            if result_ids or job_ids:
                clauses, params = [], []
                if result_ids:
                    clauses.append(f"result_id IN ({_placeholders(result_ids)})")
                    params += result_ids
                if job_ids:
                    clauses.append(f"job_id IN ({_placeholders(job_ids)})")
                    params += job_ids
                deleted = _run(
                    careerly,
                    f"DELETE FROM job_matches WHERE {' OR '.join(clauses)}",
                    tuple(params),
                )
            report.rows["careerly.db:job_matches"] = deleted

            if project_ids or job_ids:
                clauses, params = [], []
                if project_ids:
                    clauses.append(f"project_id IN ({_placeholders(project_ids)})")
                    params += project_ids
                if job_ids:
                    clauses.append(f"job_id IN ({_placeholders(job_ids)})")
                    params += job_ids
                report.rows["careerly.db:project_jobs"] = _run(
                    careerly,
                    f"DELETE FROM project_jobs WHERE {' OR '.join(clauses)}",
                    tuple(params),
                )
            else:
                report.rows["careerly.db:project_jobs"] = 0

            if project_ids:
                report.rows["careerly.db:analysis_results"] = _run(
                    careerly,
                    f"DELETE FROM analysis_results WHERE project_id IN ({_placeholders(project_ids)})",
                    tuple(project_ids),
                )
            else:
                report.rows["careerly.db:analysis_results"] = 0

            report.rows["careerly.db:analysis_projects"] = _run(
                careerly, "DELETE FROM analysis_projects WHERE user_id = ?", (user_id,)
            )
            report.rows["careerly.db:jobs"] = _run(
                careerly, "DELETE FROM jobs WHERE user_id = ?", (user_id,)
            )

            for table in _CAREERLY_DIRECT:
                report.rows[f"careerly.db:{table}"] = _run(
                    careerly, f"DELETE FROM {table} WHERE user_id = ?", (user_id,)
                )

        # ── 3. 引擎库：先子表，后 sessions ──────────────────────────────
        if engine is not None and session_ids:
            params = tuple(session_ids)
            for table in _ENGINE_BY_SESSION:
                report.rows[f"graph_state.db:{table}"] = _run(
                    engine,
                    f"DELETE FROM {table} WHERE session_id IN ({_placeholders(list(session_ids))})",
                    params,
                )
            report.rows["graph_state.db:sessions"] = _run(
                engine, "DELETE FROM sessions WHERE user_id = ?", (user_id,)
            )

        # ── 4. DemoGuard：经 metadata_json 里的 session_id 反查 ─────────
        # 该表按 visitor_id（IP）计次，没有 user 键。只有在 metadata_json
        # 里能读到该用户的 session_id 时才删 —— 不猜。
        if demo is not None and session_ids:
            matched = 0
            for sid in session_ids:
                matched += _run(
                    demo,
                    "DELETE FROM demo_usage WHERE metadata_json LIKE ?",
                    (f'%"session_id": "{sid}"%',),
                )
            report.rows["demo_usage"] = matched

        # ── 5. 落盘文件 ────────────────────────────────────────────────
        file_counts, file_failures = _purge_files(user_id, session_ids, dry_run=dry_run)
        report.files.update(file_counts)
        for failure in file_failures:
            report.warnings.append(f"文件删除失败：{failure}")

        if dry_run:
            for conn in conns:
                conn.rollback()
        else:
            for conn in conns:
                conn.commit()
    finally:
        for conn in conns:
            conn.close()

    report.orphan_artifacts = _count_orphan_artifacts(set(report.session_ids))
    if report.orphan_artifacts:
        report.warnings.append(
            f"{report.orphan_artifacts} 个产物文件无法归属到用户，**未被删除**"
            "（含 resume_parsed_*.json 这类全量 PII）—— 见 account_purge 模块文档"
        )
    return report


def _purge_files(
    user_id: int, session_ids: set[str], *, dry_run: bool
) -> tuple[dict[str, int], list[str]]:
    """删除该用户的落盘产物。

    Returns:
        (位置 → 数量, 失败说明)。删不掉的**必须**报出来 ——
        静默跳过等于对用户撒谎。
    """
    counts: dict[str, int] = {}
    failures: list[str] = []

    for directory in ARTIFACT_DIRS:
        if not directory.is_dir():
            continue
        # 用 paths.label_for 而非 relative_to(_ROOT)：数据根配到数据盘后，
        # 这些目录已在仓库树之外，直接 relative_to 会 ValueError。
        # 它前面有 is_dir() 守卫 —— 恰恰在生产数据齐全时才走到 → 删账号 500。
        label = paths.label_for(directory)
        hits = [
            entry
            for entry in directory.iterdir()
            if entry.is_file()
            and entry.name != ".gitkeep"
            and (
                _belongs_to_any(entry.name, session_ids)
                or _file_mentions_user(entry, user_id)
            )
        ]
        if not hits:
            continue
        counts[f"files:{label}"] = len(hits)
        if dry_run:
            continue
        for path in hits:
            try:
                path.unlink()
            except OSError as exc:
                failures.append(f"{label}/{path.name}: {exc}")

    return counts, failures


def _file_mentions_user(path: Path, user_id: int) -> bool:
    """文件是不是这个用户的。

    ＊ 文件名形如 ``<user_id>.json`` —— 直接认；
    ＊ 否则读 JSON 看 ``user_id`` 字段（``data/resumes/`` 下就是这个形态）。

    只对小的 ``.json`` 读内容：产物目录里有 xlsx / csv，
    读进来既慢又可能很大，且它们的归属一律走 session_id。
    """
    if path.name == f"{user_id}.json":
        return True
    if path.suffix.lower() != ".json" or path.stat().st_size > 2_000_000:
        return False
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("user_id") == user_id
