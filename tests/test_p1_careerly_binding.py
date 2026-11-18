"""P1-01 / P1-02 —— 分析结果绑定简历版本与 JD 原文快照。

覆盖：

1. 分析成功时，结果绑定**任务创建时**的当前简历版本（id + label）。
2. 分析期间用户切换简历，不会把结果改绑到新版本。
3. 修改岗位 JD 不改变已有分析记录的 JD 快照。
4. 同一岗位用不同 JD 分析，历史记录分别可追溯。
5. 迁移前的旧结果没有快照 → 明确返回 ``available=False``，
   **不**拿当前 ``jobs.raw_jd`` 冒充。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p1_careerly_binding.py -v
"""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import server as server_module
from app.careerly import router as careerly_router
from app.careerly import store as careerly_store_module
from app.careerly.engine import build_project
from app.careerly.store import CareerlyStore
from app.core import auth_store as auth_store_module
from app.models.job import JobPosting, JobSource
from app.models.match import MatchResult

_TMP_ROOT = Path(__file__).resolve().parent / ".tmp_p1_binding"


@pytest.fixture()
def workdir() -> Path:
    d = _TMP_ROOT / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def careerly_store(workdir, monkeypatch) -> CareerlyStore:
    monkeypatch.setattr(careerly_store_module, "_local", threading.local())
    store = CareerlyStore(workdir / "careerly.db")
    monkeypatch.setattr(careerly_router, "_store", store, raising=False)
    return store


@pytest.fixture()
def client(workdir, monkeypatch, careerly_store) -> TestClient:
    monkeypatch.setattr(auth_store_module, "_local", threading.local())
    monkeypatch.setattr(auth_store_module, "DB_PATH", workdir / "auth.db")
    monkeypatch.setattr(auth_store_module, "DATA_DIR", workdir)
    return TestClient(server_module.app)


def _register(client: TestClient, phone: str) -> tuple[dict[str, str], int]:
    r = client.post("/api/v1/auth/register", json={"name": "测试用户", "phone": phone})
    assert r.status_code == 201, r.text
    token = r.json()["data"]["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    return {"Authorization": f"Bearer {token}"}, int(me.json()["data"]["user"]["id"])


def _fake_project(title: str = "高级 Python 后端工程师"):
    job = JobPosting(
        title=title,
        description="负责 AI 平台后端开发",
        company="测试科技",
        required_skills=["Python", "FastAPI"],
        source=JobSource.MANUAL,
    )
    return build_project(
        title=title,
        raw_jd="负责 AI 平台后端开发，要求 Python / FastAPI",
        match_results=[MatchResult(job=job, score=0.82, skill_overlap=["Python"])],
    )


def _analyze(client, headers, **overrides):
    payload = {
        "title": "高级 Python 后端工程师",
        "rawJd": "负责 AI 平台后端开发，要求 Python / FastAPI",
        "company": "测试科技",
        "source": "paste",
    }
    payload.update(overrides)
    return client.post("/api/v1/analysis", json=payload, headers=headers)


# ── P1-01 简历版本绑定 ──────────────────────────────────────────────────────


def test_result_binds_the_resume_version_used_at_task_creation(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: (_fake_project(), {}))
    h, uid = _register(client, "13900101001")

    version = careerly_store.add_resume_version(
        uid, "张三，5 年 Python 经验", {"skills": ["Python"]}, file_name="v1.pdf"
    )

    r = _analyze(client, h)
    assert r.status_code == 201, r.text
    result = r.json()["data"]["results"][0]
    assert result["resumeVersionId"] == version["id"]
    assert result["resumeVersionLabel"] == version["label"]

    task = client.get("/api/v1/analysis/tasks", headers=h).json()["data"][0]
    assert task["resumeVersionId"] == version["id"]
    assert task["resumeVersionLabel"] == version["label"]


def test_switching_resume_mid_analysis_does_not_rebind_the_result(
    client, careerly_store, monkeypatch
):
    """任务创建时绑定 V1；分析跑到一半用户切到 V2，结果仍必须是 V1。"""
    h, uid = _register(client, "13900101002")
    v1 = careerly_store.add_resume_version(
        uid, "张三，5 年 Python 经验", {"skills": ["Python"]}
    )

    def run_and_switch(**kw):
        # 模拟「分析期间用户切换了当前简历」。
        careerly_store.add_resume_version(
            uid, "李四，3 年 Java 经验", {"skills": ["Java"]}
        )
        return _fake_project(), {}

    monkeypatch.setattr(careerly_router, "run_analysis", run_and_switch)

    r = _analyze(client, h)
    assert r.status_code == 201, r.text
    result = r.json()["data"]["results"][0]
    assert result["resumeVersionId"] == v1["id"], \
        "结果必须绑定任务创建时的 V1，而不是分析结束时的当前简历 V2"


def test_history_records_keep_their_own_resume_versions(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: (_fake_project(), {}))
    h, uid = _register(client, "13900101003")
    v1 = careerly_store.add_resume_version(uid, "V1 简历", {"skills": ["Python"]})
    first = _analyze(client, h).json()["data"]

    v2 = careerly_store.add_resume_version(uid, "V2 简历", {"skills": ["Java"]})
    second = _analyze(client, h).json()["data"]

    assert first["results"][0]["resumeVersionId"] == v1["id"]
    assert second["results"][0]["resumeVersionId"] == v2["id"]

    again = client.get(f"/api/v1/analysis/projects/{first['id']}", headers=h)
    assert again.json()["data"]["results"][0]["resumeVersionId"] == v1["id"], \
        "读取历史项目不得把简历版本换成当前版本"


# ── P1-02 JD 原文快照 ───────────────────────────────────────────────────────


def test_result_saves_jd_snapshot_with_source_metadata(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: (_fake_project(), {}))
    h, uid = _register(client, "13900101004")
    careerly_store.add_resume_version(uid, "张三，5 年 AI 产品经验", {"skills": ["AI"]})

    r = _analyze(
        client, h,
        title="AI 产品经理",
        rawJd="岗位职责：\n1. 负责 AI 产品规划\n\n任职要求：\n1. 3 年经验",
        company="示例公司",
        source="url",
        sourceUrl="https://example.com/jobs/1",
    )
    assert r.status_code == 201, r.text
    snapshot = r.json()["data"]["results"][0]["jobSnapshots"][0]
    assert snapshot["available"] is True
    assert snapshot["title"] == "AI 产品经理"
    assert snapshot["company"] == "示例公司"
    assert "任职要求" in snapshot["rawJd"]
    assert snapshot["source"] == "url"
    assert snapshot["sourceUrl"] == "https://example.com/jobs/1"
    assert snapshot["capturedAt"]


def test_editing_the_job_jd_does_not_change_existing_snapshot(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: (_fake_project(), {}))
    h, uid = _register(client, "13900101005")
    careerly_store.add_resume_version(uid, "张三，5 年 Python 经验", {"skills": ["Python"]})
    original = "原始 JD：负责 Python 后端"
    project = _analyze(client, h, rawJd=original).json()["data"]
    result_id = project["results"][0]["id"]
    job_id = project["jobs"][0]["id"]

    # 模拟后续「编辑岗位 / 重新解析链接」直接改了当前 jobs 行。
    import sqlite3
    conn = sqlite3.connect(careerly_store.db_path)
    conn.execute("UPDATE jobs SET raw_jd = ? WHERE id = ?", ("被修改后的 JD", job_id))
    conn.commit()
    conn.close()

    again = client.get(f"/api/v1/analysis/projects/{project['id']}", headers=h)
    result = again.json()["data"]["results"][0]
    assert result["id"] == result_id
    assert result["jobSnapshots"][0]["rawJd"] == original, \
        "修改岗位 JD 后，历史分析必须仍返回原始快照"
    assert again.json()["data"]["jobs"][0]["rawJd"] == "被修改后的 JD"


def test_same_job_with_two_jd_versions_is_traceable_per_analysis(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: (_fake_project(), {}))
    h, uid = _register(client, "13900101006")
    careerly_store.add_resume_version(uid, "张三，5 年经验", {"skills": ["Python"]})

    first = _analyze(client, h, rawJd="JD 版本 A：偏后端").json()["data"]
    second = _analyze(client, h, rawJd="JD 版本 B：偏平台").json()["data"]

    def snapshot_of(project_id: str) -> str:
        body = client.get(f"/api/v1/analysis/projects/{project_id}", headers=h).json()
        return body["data"]["results"][0]["jobSnapshots"][0]["rawJd"]

    assert snapshot_of(first["id"]) == "JD 版本 A：偏后端"
    assert snapshot_of(second["id"]) == "JD 版本 B：偏平台"


def test_legacy_result_without_snapshot_is_explicitly_missing(
    client, careerly_store, monkeypatch
):
    """迁移前的历史结果：返回 available=False，且 rawJd 为空。"""
    h, uid = _register(client, "13900101007")
    project = _fake_project()
    # 直接落库，不带 jobSnapshots —— 模拟功能上线前的数据。
    careerly_store.save_project(uid, project)

    loaded = careerly_store.get_project(uid, project.id)
    assert loaded is not None
    result = loaded.results[0]
    assert result.jobSnapshots, "旧结果也应显式给出缺失标记"
    assert result.jobSnapshots[0].available is False
    assert result.jobSnapshots[0].rawJd == ""
    # 当前岗位本身仍有 raw_jd —— 但它不能被当成历史原文返回。
    assert loaded.jobs[0].rawJd
