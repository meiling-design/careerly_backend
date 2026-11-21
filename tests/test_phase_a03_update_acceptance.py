"""Phase A-03 —— 「更新分析」业务闭环专项验收。

验收清单：

1. 更新追加到原项目（不新建、不覆盖），旧结果保留；
2. 全部岗位更新；
3. 只更新部分岗位 —— **未选岗位仍属于该项目**，历史完整；
4. 更换简历后更新 —— 新版本绑定正确，旧版本不受影响；
5. 修改 JD 后更新 —— 旧快照保留，新快照记录新 JD；
6. 更新失败后可重试；
7. 任务中断后可恢复，项目不被写坏。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_phase_a03_update_acceptance.py -v
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
from app.careerly.engine import AnalysisError, build_project
from app.careerly.store import CareerlyStore
from app.core import auth_store as auth_store_module
from app.models.job import JobPosting, JobSource
from app.models.match import MatchResult

_TMP_ROOT = Path(__file__).resolve().parent / ".tmp_phase_a03"

_JOBS = [
    {
        "title": "AI 产品经理",
        "rawJd": "负责 AI 产品规划与落地，要求 3 年产品经验",
        "company": "甲公司",
        "source": "paste",
    },
    {
        "title": "云原生后端工程师",
        "rawJd": "负责 Kubernetes 平台建设，精通 Go 与容器编排",
        "company": "乙公司",
        "source": "paste",
    },
    {
        "title": "数据分析师",
        "rawJd": "负责指标体系与 SQL 分析，要求统计学背景",
        "company": "丙公司",
        "source": "paste",
    },
]


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


def _auth(client: TestClient, phone: str) -> tuple[dict[str, str], int]:
    r = client.post("/api/v1/auth/register", json={"name": "验收用户", "phone": phone})
    assert r.status_code == 201, r.text
    token = r.json()["data"]["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = client.get("/api/v1/auth/me", headers=headers).json()
    return headers, int(me["data"]["user"]["id"])


def _fake_multi_project(jobs, scores=None):
    if scores is None:
        scores = [0.9, 0.7, 0.5]
    match_results = []
    for i, j in enumerate(jobs):
        job = JobPosting(
            title=j["title"],
            description=j["rawJd"],
            company=j.get("company") or "",
            required_skills=[],
            source=JobSource.MANUAL,
        )
        match_results.append(
            MatchResult(job=job, score=scores[i % len(scores)], skill_overlap=[])
        )
    return build_project(jobs=jobs, match_results=match_results), {}


def _analyze(client, headers, jobs):
    return client.post(
        "/api/v1/analysis",
        json={"resumeText": "", "jobs": jobs},
        headers=headers,
    )


def _initial_project(client, headers, uid, store, resume_label="V1"):
    """建一份带 3 个岗位的项目（V1 作基线）。"""
    version = store.add_resume_version(
        uid, f"简历 {resume_label}：3 年 AI 产品经验", {"skills": ["AI"]}
    )
    created = _analyze(client, headers, _JOBS)
    assert created.status_code == 201, created.text
    return created.json()["data"], version


def _job_refs(project, titles=None):
    """把项目岗位转成更新请求的 jobs 入参（可只选一部分）。"""
    return [
        {
            "title": job["title"],
            "rawJd": job["rawJd"],
            "company": job["company"],
            "jobId": job["id"],
            "source": "paste",
        }
        for job in project["jobs"]
        if titles is None or job["title"] in titles
    ]


def _update(client, headers, project, titles=None, resume_version_id=None, overrides=None):
    jobs = _job_refs(project, titles)
    if overrides:
        for job in jobs:
            job.update(overrides.get(job["title"], {}))
    return client.post(
        "/api/v1/analysis",
        json={
            "projectId": project["id"],
            "resumeVersionId": resume_version_id,
            "jobs": jobs,
        },
        headers=headers,
    )


# ── 1 + 2. 全部岗位更新：追加版本，保留旧结果 ───────────────────────────────


def test_full_update_appends_a_version_without_touching_history(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(
        careerly_router, "run_analysis",
        lambda **kw: _fake_multi_project(kw["jobs"]),
    )
    headers, uid = _auth(client, "13900401001")
    project, v1 = _initial_project(client, headers, uid, careerly_store)

    v2 = careerly_store.add_resume_version(
        uid, "简历 V2：5 年产品 + 数据分析", {"skills": ["AI", "SQL"]}
    )
    updated = _update(
        client, headers, project, resume_version_id=v2["id"]
    )
    assert updated.status_code == 201, updated.text
    body = updated.json()["data"]

    # 同一个项目，版本递增
    assert body["id"] == project["id"], "更新不得创建新项目"
    assert [r["version"] for r in body["results"]] == [1, 2]
    assert len(body["results"][0]["matches"]) == 3, "旧结果必须原样保留"
    assert len(body["results"][1]["matches"]) == 3

    # 本次更新绑定新简历，旧版本仍是旧简历
    assert body["results"][1]["resumeVersionId"] == v2["id"]
    assert body["results"][0]["resumeVersionId"] == v1["id"]

    # 回读一致
    again = client.get(f"/api/v1/analysis/projects/{project['id']}", headers=headers)
    assert again.status_code == 200
    assert [r["version"] for r in again.json()["data"]["results"]] == [1, 2]

    conn = __import__("sqlite3").connect(careerly_store.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM analysis_projects"
        ).fetchone()[0] == 1, "更新不得留下第二个项目"
    finally:
        conn.close()


# ── 3. 部分岗位更新：未选岗位仍属于项目 ─────────────────────────────────────


def test_partial_update_keeps_unselected_jobs_in_the_project(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(
        careerly_router, "run_analysis",
        lambda **kw: _fake_multi_project(kw["jobs"]),
    )
    headers, uid = _auth(client, "13900401002")
    project, _ = _initial_project(client, headers, uid, careerly_store)
    b_id = next(j["id"] for j in project["jobs"] if j["title"] == "云原生后端工程师")
    b_raw = next(j["rawJd"] for j in project["jobs"] if j["title"] == "云原生后端工程师")

    updated = _update(client, headers, project, titles={"AI 产品经理", "数据分析师"})
    assert updated.status_code == 201, updated.text
    body = updated.json()["data"]

    # 版本 2 只包含本次更新的 A、C
    assert [r["version"] for r in body["results"]] == [1, 2]
    v2_titles = {
        j["title"] for j in body["jobs"]
        if any(
            m["jobId"] == j["id"] for m in body["results"][1]["matches"]
        )
    }
    assert v2_titles == {"AI 产品经理", "数据分析师"}

    # ⭐ 未选岗位 B 仍必须挂在项目上 —— 更新是「重新分析一部分」，
    #    不是「把没选的岗位从项目里移除」。
    all_job_ids = {j["id"] for j in body["jobs"]}
    assert b_id in all_job_ids, "部分更新后未选岗位不得从项目中消失"
    assert len(body["jobs"]) == 3
    assert next(j for j in body["jobs"] if j["id"] == b_id)["rawJd"] == b_raw

    # 版本 1（含 B 的匹配与 JD 快照）原样保留
    v1 = body["results"][0]
    assert {m["jobId"] for m in v1["matches"]} == {
        j["id"] for j in project["jobs"]
    }
    b_snap = next(s for s in v1["jobSnapshots"] if s["jobId"] == b_id)
    assert b_snap["available"] is True
    assert b_snap["rawJd"] == b_raw

    # 回读也一致（这是前端重新进入详情页看到的数据）
    again = client.get(f"/api/v1/analysis/projects/{project['id']}", headers=headers)
    persisted = again.json()["data"]
    assert len(persisted["jobs"]) == 3
    assert b_id in {j["id"] for j in persisted["jobs"]}
    assert [r["version"] for r in persisted["results"]] == [1, 2]


# ── 4. 更换简历后的更新 ─────────────────────────────────────────────────────


def test_update_with_a_different_resume_binds_and_compares_correctly(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(
        careerly_router, "run_analysis",
        lambda **kw: _fake_multi_project(kw["jobs"]),
    )
    headers, uid = _auth(client, "13900401003")
    project, v1 = _initial_project(client, headers, uid, careerly_store)
    v2 = careerly_store.add_resume_version(
        uid, "简历 V2：5 年产品 + 数据分析", {"skills": ["AI", "SQL"]}
    )

    updated = _update(client, headers, project, resume_version_id=v2["id"])
    assert updated.status_code == 201, updated.text
    body = updated.json()["data"]

    assert body["results"][1]["resumeVersionId"] == v2["id"]
    assert body["results"][1]["resumeVersionLabel"] == v2["label"]
    assert body["results"][1]["resumeFingerprint"] != \
        body["results"][0]["resumeFingerprint"], "换了简历，指纹必须不同"

    # 同一岗位 + 同一 JD + 不同简历 → 后端给出可比较的 previousScore
    for match in body["results"][1]["matches"]:
        old = next(
            m for m in body["results"][0]["matches"] if m["jobId"] == match["jobId"]
        )
        assert match["previousScore"] == old["score"]


# ── 5. 修改 JD 后的更新 ─────────────────────────────────────────────────────


def test_update_after_editing_jd_keeps_old_snapshot(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(
        careerly_router, "run_analysis",
        lambda **kw: _fake_multi_project(kw["jobs"]),
    )
    headers, uid = _auth(client, "13900401004")
    project, _ = _initial_project(client, headers, uid, careerly_store)

    a_job = next(j for j in project["jobs"] if j["title"] == "AI 产品经理")
    old_jd = a_job["rawJd"]
    new_jd = "修改后的 JD：负责 AI 产品商业化与增长"

    updated = _update(
        client, headers, project, titles={"AI 产品经理"},
        overrides={"AI 产品经理": {"rawJd": new_jd}},
    )
    assert updated.status_code == 201, updated.text
    body = updated.json()["data"]

    v1 = body["results"][0]
    v2 = body["results"][1]

    # 旧快照仍是旧 JD；新快照记录新 JD
    assert next(s for s in v1["jobSnapshots"] if s["jobId"] == a_job["id"])["rawJd"] == old_jd
    assert next(s for s in v2["jobSnapshots"] if s["jobId"] == a_job["id"])["rawJd"] == new_jd

    # 当前岗位实体已更新，但历史不跟着变
    assert next(j for j in body["jobs"] if j["id"] == a_job["id"])["rawJd"] == new_jd
    again = client.get(f"/api/v1/analysis/projects/{project['id']}", headers=headers)
    persisted = again.json()["data"]
    assert next(
        s for s in persisted["results"][0]["jobSnapshots"] if s["jobId"] == a_job["id"]
    )["rawJd"] == old_jd, "修改 JD 后旧分析原文不得被覆盖"

    # JD 变了 → 不可比较，不得出现虚假提升/下降
    assert all(m["previousScore"] is None for m in v2["matches"])

    # 未选岗位仍在项目里
    assert len(body["jobs"]) == 3


# ── 6. 更新失败后重试 ───────────────────────────────────────────────────────


def test_failed_update_can_be_retried_without_corrupting_project(
    client, careerly_store, monkeypatch
):
    state = {"fail": False}

    def flaky(**kw):
        if state["fail"]:
            raise AnalysisError("ANALYSIS_FAILED", "模拟更新失败", 502)
        return _fake_multi_project(kw["jobs"])

    monkeypatch.setattr(careerly_router, "run_analysis", flaky)
    headers, uid = _auth(client, "13900401005")
    project, v1 = _initial_project(client, headers, uid, careerly_store)

    state["fail"] = True
    failed = _update(client, headers, project)
    assert failed.status_code == 502, failed.text

    # 失败：项目没有任何变化，额度已释放
    unchanged = client.get(
        f"/api/v1/analysis/projects/{project['id']}", headers=headers
    ).json()["data"]
    assert [r["version"] for r in unchanged["results"]] == [1]
    assert len(unchanged["jobs"]) == 3
    assert client.get("/api/v1/quota", headers=headers).json()["data"]["reserved"] == 0

    tasks = client.get("/api/v1/analysis/tasks", headers=headers).json()["data"]
    assert tasks[0]["status"] == "failed"
    assert tasks[0]["projectId"] is None

    # 重试成功 → 只追加一个版本，不会因为失败留下半份数据
    state["fail"] = False
    retried = _update(client, headers, project)
    assert retried.status_code == 201, retried.text
    assert [r["version"] for r in retried.json()["data"]["results"]] == [1, 2]
    assert client.get("/api/v1/quota", headers=headers).json()["data"]["used"] == 2


# ── 7. 任务中断后恢复 ───────────────────────────────────────────────────────


def test_interrupted_update_recovers_and_project_stays_intact(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(
        careerly_router, "run_analysis",
        lambda **kw: _fake_multi_project(kw["jobs"]),
    )
    headers, uid = _auth(client, "13900401006")
    project, _ = _initial_project(client, headers, uid, careerly_store)

    store = careerly_store
    task_id = store.create_task(
        uid,
        "AI 产品经理 等 3 个岗位",
        source="paste",
        jd_snapshot={"jobs": _JOBS},
    )
    assert store.reserve_analysis_quota(uid, task_id) in (
        store.QUOTA_RESERVED, store.QUOTA_MEMBER,
    )
    assert store.start_task(task_id) is True

    # 进程重启
    assert store.recover_interrupted_tasks() == 1
    task = client.get(f"/api/v1/analysis/tasks/{task_id}", headers=headers).json()["data"]
    assert task["status"] == "failed"
    assert task["errorCode"] == "TASK_INTERRUPTED"

    # 原项目完全不受影响
    unchanged = client.get(
        f"/api/v1/analysis/projects/{project['id']}", headers=headers
    ).json()["data"]
    assert [r["version"] for r in unchanged["results"]] == [1]
    assert len(unchanged["jobs"]) == 3
    quota = client.get("/api/v1/quota", headers=headers).json()["data"]
    assert quota["used"] == 1 and quota["reserved"] == 0

    # 中断后重试：正常追加版本 2
    retried = _update(client, headers, project, titles={"AI 产品经理", "数据分析师"})
    assert retried.status_code == 201, retried.text
    body = retried.json()["data"]
    assert [r["version"] for r in body["results"]] == [1, 2]
    assert len(body["jobs"]) == 3, "重试同样不得丢掉未选岗位"
