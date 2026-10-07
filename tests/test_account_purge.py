"""账号级联清除测试（决策 D119 / 发布基线 K19）。

这里检验的是**两条相反方向**的性质：

```text
删干净    该用户的数据一间不留 —— 漏一张表 = 用户看到「已删除」但简历还在
不误删    别的用户的数据一行不动 —— 谓词写宽 = 直接毁掉无关用户的数据
```

第二条比第一条更危险。第一条漏了会被发现（用户投诉「怎么还在」），
第二条只会安静地发生：某天删一个测试账号，顺手删掉了另外几个人的简历版本。

因此下面每个用例都同时断言「该没的没了」和「该在的还在」。

表结构不写死在测试里，而是从**实际运行的库**里读 ``sqlite_master`` 复制过来 ——
否则某天加了新表，测试会继续在旧结构上通过，等于没有保护。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.core import account_purge

# 被测的两张表都用 TEXT 主键（业务 id 形如 proj-xxx），插入时给足列。
_USER = 1
_OTHER = 2


def _clone_schema(src: Path, dst: Path) -> None:
    """把 *src* 的全部表结构复制到 *dst*（不复制数据）。"""
    source = sqlite3.connect(str(src))
    target = sqlite3.connect(str(dst))
    try:
        # 跳过 sqlite_* —— sqlite_sequence 是 AUTOINCREMENT 的内部表，
        # 手工 CREATE 会报 "object name reserved for internal use"，
        # 而它由目标库自己按需重建。
        for (sql,) in source.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%'"
        ):
            target.execute(sql)
        target.commit()
    finally:
        source.close()
        target.close()


def _seed(conn: sqlite3.Connection, user_id: int) -> dict[str, str]:
    """给 *user_id* 造一套完整数据，返回它的 project / job / result id。"""
    now = time.time()
    suffix = f"u{user_id}"
    project, job, result = f"proj-{suffix}", f"job-{suffix}", f"res-{suffix}"

    conn.execute(
        "INSERT INTO analysis_projects (id, user_id, name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (project, user_id, f"项目 {suffix}", now, now),
    )
    conn.execute(
        "INSERT INTO jobs (id, user_id, title, company, raw_jd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job, user_id, f"职位 {suffix}", "ACME", "JD 原文", now),
    )
    conn.execute(
        "INSERT INTO analysis_results (id, project_id, version, created_at) "
        "VALUES (?, ?, ?, ?)",
        (result, project, 1, now),
    )
    conn.execute(
        "INSERT INTO project_jobs (project_id, job_id, position) VALUES (?, ?, 0)",
        (project, job),
    )
    conn.execute(
        "INSERT INTO job_matches (result_id, job_id, rank, score) VALUES (?, ?, 1, 80)",
        (result, job),
    )
    conn.execute(
        "INSERT INTO careerly_resumes (user_id, text, updated_at) VALUES (?, ?, ?)",
        (user_id, f"简历全文 {suffix}", now),
    )
    conn.commit()
    return {"project": project, "job": job, "result": result}


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """把 account_purge 指向一套临时库，并预置两个用户的数据。"""
    live_root = Path(__file__).resolve().parent.parent
    paths = {
        "AUTH_DB": tmp_path / "auth.db",
        "CAREERLY_DB": tmp_path / "careerly.db",
        "ENGINE_DB": tmp_path / "engine.db",
        "DEMO_DB": tmp_path / "demo.db",
    }
    sources = {
        "AUTH_DB": live_root / "app" / "data" / "auth.db",
        "CAREERLY_DB": live_root / "app" / "data" / "careerly.db",
        "ENGINE_DB": live_root / "app" / "data" / "graph_state.db",
        "DEMO_DB": live_root / "data" / "graph_state.db",
    }
    for attr, dst in paths.items():
        if not sources[attr].exists():
            pytest.skip(f"缺少 {sources[attr]} —— 无法复制表结构")
        _clone_schema(sources[attr], dst)
        monkeypatch.setattr(account_purge, attr, dst)

    # 产物目录指向空目录：本测试只验数据库侧，文件侧由 _purge_files 单独覆盖。
    monkeypatch.setattr(account_purge, "ARTIFACT_DIRS", ())

    careerly = sqlite3.connect(str(paths["CAREERLY_DB"]))
    engine = sqlite3.connect(str(paths["ENGINE_DB"]))
    demo = sqlite3.connect(str(paths["DEMO_DB"]))
    try:
        mine = _seed(careerly, _USER)
        theirs = _seed(careerly, _OTHER)
        # 引擎库：每个用户一个 session（user_id 是真实存在的列）
        engine.execute(
            "INSERT INTO sessions (session_id, created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?)",
            (f"session-u{_USER}", time.time(), time.time(), _USER),
        )
        engine.execute(
            "INSERT INTO sessions (session_id, created_at, updated_at, user_id) "
            "VALUES (?, ?, ?, ?)",
            (f"session-u{_OTHER}", time.time(), time.time(), _OTHER),
        )
        engine.execute(
            "INSERT INTO resume (session_id, resume_json) VALUES (?, ?)",
            (f"session-u{_USER}", "我的简历"),
        )
        engine.execute(
            "INSERT INTO resume (session_id, resume_json) VALUES (?, ?)",
            (f"session-u{_OTHER}", "别人的简历"),
        )
        engine.commit()
    finally:
        careerly.close()
        engine.close()
        demo.close()

    yield paths


def _count(db: Path, table: str, where: str = "", params: tuple = ()) -> int:
    conn = sqlite3.connect(str(db))
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def test_dry_run_changes_nothing(sandbox) -> None:
    """预演必须**一行都不动** —— 它是给人做决定用的。"""
    before = _count(sandbox["CAREERLY_DB"], "analysis_projects")
    report = account_purge.purge_user(_USER, dry_run=True)

    assert report.total_rows > 0, "预演也该报出将删除的数量"
    assert _count(sandbox["CAREERLY_DB"], "analysis_projects") == before


def test_apply_removes_the_target_user(sandbox) -> None:
    report = account_purge.purge_user(_USER, dry_run=False)
    assert report.total_rows > 0

    careerly = sandbox["CAREERLY_DB"]
    assert _count(careerly, "analysis_projects", "user_id = ?", (_USER,)) == 0
    assert _count(careerly, "jobs", "user_id = ?", (_USER,)) == 0
    assert _count(careerly, "careerly_resumes", "user_id = ?", (_USER,)) == 0
    # join 表没有 user_id，只能经 project / result 反查 —— 这三张最容易漏
    assert _count(careerly, "analysis_results", "project_id LIKE 'proj-u1'") == 0
    assert _count(careerly, "project_jobs", "project_id LIKE 'proj-u1'") == 0
    assert _count(careerly, "job_matches", "job_id LIKE 'job-u1'") == 0

    engine = sandbox["ENGINE_DB"]
    assert _count(engine, "sessions", "user_id = ?", (_USER,)) == 0
    assert _count(engine, "resume", "session_id = ?", (f"session-u{_USER}",)) == 0


def test_other_users_data_survives(sandbox) -> None:
    """⚠️ 本文件最重要的一条：删 A 不能碰到 B。"""
    account_purge.purge_user(_USER, dry_run=False)

    careerly = sandbox["CAREERLY_DB"]
    assert _count(careerly, "analysis_projects", "user_id = ?", (_OTHER,)) == 1
    assert _count(careerly, "jobs", "user_id = ?", (_OTHER,)) == 1
    assert _count(careerly, "careerly_resumes", "user_id = ?", (_OTHER,)) == 1
    assert _count(careerly, "analysis_results", "project_id LIKE 'proj-u2'") == 1
    assert _count(careerly, "project_jobs", "project_id LIKE 'proj-u2'") == 1
    assert _count(careerly, "job_matches", "job_id LIKE 'job-u2'") == 1

    engine = sandbox["ENGINE_DB"]
    assert _count(engine, "sessions", "user_id = ?", (_OTHER,)) == 1
    assert _count(engine, "resume", "session_id = ?", (f"session-u{_OTHER}",)) == 1


def test_session_ids_are_collected_for_file_matching(sandbox) -> None:
    """session_id 是产物文件唯一的定位线索，必须在删除前被取出来。"""
    report = account_purge.purge_user(_USER, dry_run=True)
    assert report.session_ids == [f"session-u{_USER}"]


def test_reports_unattributable_artifacts(sandbox, monkeypatch: pytest.MonkeyPatch) -> None:
    """产物文件删不到时必须**说出来** —— 静默成功等于对用户撒谎。"""
    outputs = sandbox["CAREERLY_DB"].parent / "outputs"
    outputs.mkdir()
    (outputs / "report_session-someone-else.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(account_purge, "_ROOT", sandbox["CAREERLY_DB"].parent)
    monkeypatch.setattr(account_purge, "ARTIFACT_DIRS", (outputs,))

    report = account_purge.purge_user(_USER, dry_run=False)
    assert report.orphan_artifacts == 1
    assert any("无法归属到用户" in w for w in report.warnings)
    assert (outputs / "report_session-someone-else.json").exists(), "不属于该用户的文件不得被删"


def test_without_sessions_warns(sandbox) -> None:
    """没有 session 可归属时，产物文件定位不了 —— 必须给出警告而不是当作已完成。"""
    report = account_purge.purge_user(999_999, dry_run=True)
    assert report.total_rows == 0
    assert any("session" in w for w in report.warnings)
