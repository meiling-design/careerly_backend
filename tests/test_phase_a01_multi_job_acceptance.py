"""Phase A-01 —— 多岗位分析专项验收。

覆盖验收清单里现有用例没有钉住的部分：

1. 一次提交 3 个岗位，全部成功 —— 每个岗位一条匹配、统一排名、
   JD 快照一一对应、持久化后不重复不多写。
2. 单岗位失败（引擎走 ``_fallback_result``）—— 其余岗位不受影响，
   不出现结果错位 / 覆盖。
3. 全部岗位失败 —— 任务明确失败，**不落半个 project**，额度释放。
4. 任务中断 —— 重启恢复把 qued/processing 任务标成 failed 并释放额度，
   不产生残缺数据；之后可以正常重试。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_phase_a01_multi_job_acceptance.py -v
"""

from __future__ import annotations

import sqlite3
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

_TMP_ROOT = Path(__file__).resolve().parent / ".tmp_phase_a01"


# ── Fixtures（与既有 P1 用例同构：零删除，uuid 目录用完即弃）───────────────


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


# ── 构造引擎产物 ────────────────────────────────────────────────────────────

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


def _result_for(jd: str, title: str, company: str, score: float) -> MatchResult:
    return MatchResult(
        job=JobPosting(
            title=title,
            description=jd,
            company=company,
            required_skills=[],
            source=JobSource.MANUAL,
        ),
        score=score,
        skill_overlap=[],
    )


def _fake_multi_project(jobs, scores=None):
    """按本次入参构造一个多岗位 project（不跑真实引擎）。"""
    by_title = {j["title"]: j for j in jobs}
    assert set(by_title) <= {j["title"] for j in _JOBS}, jobs
    if scores is None:
        scores = [0.9, 0.7, 0.5]
    match_results = [
        _result_for(j["rawJd"], j["title"], j["company"], score)
        for j, score in zip(jobs, scores)
    ]
    return build_project(jobs=jobs, match_results=match_results), {}


def _fallback_for(jd: str, title: str, company: str) -> MatchResult:
    """MatchingAgent._fallback_result 的等价形态：0 分 + 明确错误理由。"""
    return MatchResult(
        job=JobPosting(
            title=title, description=jd, company=company,
            required_skills=[], source=JobSource.MANUAL,
        ),
        score=0.0,
        match_reasons=["⚠️ Matching encountered an error"],
        recommendations=["Please retry matching"],
    )


def _analyze(client, headers, jobs=None):
    return client.post(
        "/api/v1/analysis",
        json={
            "resumeText": "张三，5 年产品与后端经验",
            "jobs": jobs if jobs is not None else _JOBS,
        },
        headers=headers,
    )


# ── 1. 三个岗位全部成功 ─────────────────────────────────────────────────────


def test_three_jobs_all_succeed_without_misalignment(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(
        careerly_router, "run_analysis",
        lambda **kw: _fake_multi_project(kw["jobs"]),
    )
    headers, _ = _auth(client, "13900201001")

    r = _analyze(client, headers)
    assert r.status_code == 201, r.text
    project = r.json()["data"]

    # 输入 3 个岗位 → 3 个 job 实体、3 条匹配、3 份快照
    assert len(project["jobs"]) == 3, project["jobs"]
    result = project["results"][0]
    assert len(result["matches"]) == 3, result["matches"]
    assert len(result["jobSnapshots"]) == 3, result["jobSnapshots"]

    # 统一排名 1..3，且与分数同序
    by_rank = sorted(result["matches"], key=lambda m: m["rank"])
    assert [m["rank"] for m in by_rank] == [1, 2, 3]
    assert [m["score"] for m in by_rank] == [90, 70, 50]

    # 每条匹配、每份快照都准确落回自己的岗位（按 jobId 对齐）
    job_by_id = {j["id"]: j for j in project["jobs"]}
    snap_by_id = {s["jobId"]: s for s in result["jobSnapshots"]}
    for match in result["matches"]:
        job = job_by_id[match["jobId"]]
        snap = snap_by_id[match["jobId"]]
        assert snap["title"] == job["title"]
        assert snap["rawJd"] == job["rawJd"]
        assert snap["company"] == job["company"]

    # 持久化后重读：数据逐字一致，不重复写入
    again = client.get(f"/api/v1/analysis/projects/{project['id']}", headers=headers)
    assert again.status_code == 200, again.text
    persisted = again.json()["data"]
    assert persisted == project, "GET 回读必须与首次响应一致（无错位/覆盖）"

    # 任务 completed，只扣一次额度
    tasks = client.get("/api/v1/analysis/tasks", headers=headers).json()["data"]
    assert len(tasks) == 1
    assert tasks[0]["status"] == "completed"
    assert tasks[0]["projectId"] == project["id"]
    assert client.get("/api/v1/quota", headers=headers).json()["data"]["used"] == 1

    # 库内不重复：3 job / 1 result / 3 match 行
    conn = sqlite3.connect(careerly_store.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM analysis_results").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM job_matches").fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM job_matches GROUP BY result_id, job_id "
            "HAVING COUNT(*) > 1"
        ).fetchall() == []
    finally:
        conn.close()


# ── 2. 单个岗位失败（降级）不影响其他岗位 ───────────────────────────────────


def test_one_job_failure_does_not_affect_the_others(
    client, careerly_store, monkeypatch
):
    def partial_failure(**kw):
        jobs = kw["jobs"]
        assert len(jobs) == 3
        results = [
            _result_for(jobs[0]["rawJd"], jobs[0]["title"], jobs[0]["company"], 0.9),
            _fallback_for(jobs[1]["rawJd"], jobs[1]["title"], jobs[1]["company"]),
            _result_for(jobs[2]["rawJd"], jobs[2]["title"], jobs[2]["company"], 0.5),
        ]
        return build_project(jobs=jobs, match_results=results), {}

    monkeypatch.setattr(careerly_router, "run_analysis", partial_failure)
    headers, _ = _auth(client, "13900201002")

    r = _analyze(client, headers)
    assert r.status_code == 201, r.text
    project = r.json()["data"]
    result = project["results"][0]

    # 仍是 3 个岗位、3 条匹配 —— 部分失败不能被吞成缺行
    assert len(project["jobs"]) == 3
    assert len(result["matches"]) == 3

    scores = {m["jobId"]: m["score"] for m in result["matches"]}
    title_by_id = {j["id"]: j["title"] for j in project["jobs"]}
    assert scores == {
        next(j["id"] for j in project["jobs"] if j["title"] == "AI 产品经理"): 90,
        next(j["id"] for j in project["jobs"] if j["title"] == "云原生后端工程师"): 0,
        next(j["id"] for j in project["jobs"] if j["title"] == "数据分析师"): 50,
    }, "失败岗位之外的分数不得被污染"

    # 失败岗位保留可辨识的降级理由；其他岗位仍是正常结果
    reasons = {title_by_id[m["jobId"]]: m["reasons"] for m in result["matches"]}
    assert any("error" in x.lower() for x in reasons["云原生后端工程师"])
    assert not any("error" in x.lower() for x in reasons["AI 产品经理"])

    # 排名是按分数全局排的：0 分岗位垫底，不抢占其他岗位名次
    rank_by_title = {
        title_by_id[m["jobId"]]: m["rank"] for m in result["matches"]
    }
    assert rank_by_title["AI 产品经理"] == 1
    assert rank_by_title["数据分析师"] == 2
    assert rank_by_title["云原生后端工程师"] == 3

    # 降级也算一次成功分析，任务 completed、结果可回读
    task = client.get("/api/v1/analysis/tasks", headers=headers).json()["data"][0]
    assert task["status"] == "completed"
    again = client.get(f"/api/v1/analysis/projects/{project['id']}", headers=headers)
    assert again.status_code == 200


# ── 3. 全部岗位失败 ─────────────────────────────────────────────────────────


def test_all_jobs_failing_leaves_no_partial_project(
    client, careerly_store, monkeypatch
):
    def all_fail(**kw):
        raise AnalysisError("ANALYSIS_EMPTY", "未能产出匹配结果：全部岗位分析失败", 422)

    monkeypatch.setattr(careerly_router, "run_analysis", all_fail)
    headers, _ = _auth(client, "13900201003")

    r = _analyze(client, headers)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "ANALYSIS_EMPTY"

    # 不落半个 project
    assert client.get("/api/v1/analysis/projects", headers=headers).json()["data"] == []
    assert client.get("/api/v1/quota", headers=headers).json()["data"]["used"] == 0

    task = client.get("/api/v1/analysis/tasks", headers=headers).json()["data"][0]
    assert task["status"] == "failed"
    assert task["errorCode"] == "ANALYSIS_EMPTY"
    assert task["projectId"] is None

    conn = sqlite3.connect(careerly_store.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM analysis_projects").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM job_matches").fetchone()[0] == 0
    finally:
        conn.close()


# ── 4. 任务中断与恢复 ───────────────────────────────────────────────────────


def test_interrupted_multi_job_task_recovers_and_can_retry(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(
        careerly_router, "run_analysis",
        lambda **kw: _fake_multi_project(kw["jobs"]),
    )
    headers, uid = _auth(client, "13900201004")

    # 模拟「任务已建、额度已预占、引擎跑到一半进程被杀」。
    store = careerly_store
    task_id = store.create_task(
        uid,
        "AI 产品经理 等 3 个岗位",
        source="paste",
        jd_snapshot={"jobs": _JOBS},
    )
    assert store.reserve_analysis_quota(uid, task_id) in (
        CareerlyStore.QUOTA_RESERVED,
        CareerlyStore.QUOTA_MEMBER,
    )
    assert store.start_task(task_id) is True
    assert store.get_quota(uid).used == 0
    assert store.get_quota(uid).reserved == 1

    # 启动恢复：任务标失败、预占释放，且不自动重跑（可能重复扣费）。
    assert store.recover_interrupted_tasks() == 1

    task = client.get(f"/api/v1/analysis/tasks/{task_id}", headers=headers)
    assert task.status_code == 200
    body = task.json()["data"]
    assert body["status"] == "failed"
    assert body["errorCode"] == "TASK_INTERRUPTED"
    assert body["projectId"] is None

    quota = client.get("/api/v1/quota", headers=headers).json()["data"]
    assert quota["used"] == 0
    assert quota["reserved"] == 0
    assert client.get("/api/v1/analysis/projects", headers=headers).json()["data"] == []

    # 中断后用户重试同一批岗位：应正常成功，且只落一份完整结果。
    r = _analyze(client, headers)
    assert r.status_code == 201, r.text
    project = r.json()["data"]
    assert len(project["jobs"]) == 3
    assert len(project["results"][0]["matches"]) == 3
    assert client.get("/api/v1/quota", headers=headers).json()["data"]["used"] == 1


# ── 5. 重复 JD 不会制造重复岗位 / 错位匹配 ──────────────────────────────────


def test_duplicate_jd_is_deduplicated_without_misalignment(
    client, careerly_store, monkeypatch
):
    """同一份 JD 提交两次：只建一个岗位，匹配仍准确落位。

    前端岗位库已经会拦截重复 JD（``phase6_multi_job_analysis_test``），
    但服务端不能依赖客户端 —— 这里钉住后端的去重与映射。
    """
    monkeypatch.setattr(
        careerly_router, "run_analysis",
        lambda **kw: _fake_multi_project(kw["jobs"][:2]),
    )
    headers, _ = _auth(client, "13900201005")

    duplicated = [
        _JOBS[0],
        dict(_JOBS[1]),           # 同一份 JD、不同岗位名
        _JOBS[2],
    ]
    r = _analyze(client, headers, jobs=duplicated)
    assert r.status_code == 201, r.text
    project = r.json()["data"]

    # 服务端按 JD 原文去重：只剩 2 个岗位
    assert len(project["jobs"]) == 2, project["jobs"]
    assert len({j["rawJd"] for j in project["jobs"]}) == 2

    # 匹配数与岗位数一致，且每条匹配都指向真实存在的 jobId
    result = project["results"][0]
    job_ids = {j["id"] for j in project["jobs"]}
    assert len(result["matches"]) == 2
    assert {m["jobId"] for m in result["matches"]} <= job_ids
    assert sorted(m["rank"] for m in result["matches"]) == [1, 2]
