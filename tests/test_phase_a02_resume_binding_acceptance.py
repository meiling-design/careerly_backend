"""Phase A-02 —— 简历版本与分析结果绑定的专项回归。

验收清单里现有用例没覆盖的两条：

1. 简历 A → 分析 → 简历 B → 分析 → 切回 A → 再分析：
   三次分析分别绑定 A / B / A，历史结果永不改写。
2. 历史版本被删除后：历史分析仍保留当时的版本 id / 标签 / 指纹，
   不依赖已不存在的简历行，也不回退到当前简历。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_phase_a02_resume_binding_acceptance.py -v
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

_TMP_ROOT = Path(__file__).resolve().parent / ".tmp_phase_a02"


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


def _fake_project():
    jd = "负责 AI 产品规划与落地"
    job = JobPosting(
        title="AI 产品经理",
        description=jd,
        company="甲公司",
        required_skills=["AI"],
        source=JobSource.MANUAL,
    )
    return build_project(
        title="AI 产品经理",
        raw_jd=jd,
        match_results=[MatchResult(job=job, score=0.8, skill_overlap=[])],
    )


def _analyze(client, headers):
    return client.post(
        "/api/v1/analysis",
        json={
            "title": "AI 产品经理",
            "rawJd": "负责 AI 产品规划与落地",
        },
        headers=headers,
    )


# ── 1. A → B → A 三次分析 ───────────────────────────────────────────────────


def test_a_b_a_analyses_each_bind_their_own_resume_version(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: (_fake_project(), {}))
    headers, uid = _auth(client, "13900301001")

    # 简历 A
    a = careerly_store.add_resume_version(uid, "简历 A：3 年 AI 产品", {"skills": ["AI"]})
    first = _analyze(client, headers)
    assert first.status_code == 201, first.text
    first_project = first.json()["data"]

    # 简历 B（add_resume_version 自动提为当前）
    b = careerly_store.add_resume_version(uid, "简历 B：5 年后端", {"skills": ["Go"]})
    second = _analyze(client, headers)
    assert second.status_code == 201, second.text
    second_project = second.json()["data"]

    # 切回 A 后再分析
    switched = client.put(
        f"/api/v1/resume/versions/{a['id']}/current", headers=headers
    )
    assert switched.status_code == 200, switched.text
    third = _analyze(client, headers)
    assert third.status_code == 201, third.text
    third_project = third.json()["data"]

    expected = [
        (first_project, a),
        (second_project, b),
        (third_project, a),
    ]
    for project, version in expected:
        result = project["results"][0]
        assert result["resumeVersionId"] == version["id"]
        assert result["resumeVersionLabel"] == version["label"]
        assert result["resumeFingerprint"], "每次分析都要写下简历指纹"

    # 三次分析各自是独立项目，且历史绑定在全部完成后仍不变
    assert len({p["id"] for p, _ in expected}) == 3
    for project, version in expected:
        again = client.get(
            f"/api/v1/analysis/projects/{project['id']}", headers=headers
        )
        assert again.status_code == 200, again.text
        result = again.json()["data"]["results"][0]
        assert result["resumeVersionId"] == version["id"], \
            "切回 A 不得改写前两次历史分析的绑定"
        assert result["resumeVersionLabel"] == version["label"]

    # 任务行同样保留各自绑定
    tasks = client.get("/api/v1/analysis/tasks", headers=headers).json()["data"]
    assert len(tasks) == 3
    assert {(t["resumeVersionId"], t["resumeVersionLabel"]) for t in tasks} == {
        (a["id"], a["label"]),
        (b["id"], b["label"]),
    }


# ── 2. 删除历史版本后历史分析仍可追溯 ───────────────────────────────────────


def test_deleting_history_version_keeps_analysis_traceable(
    client, careerly_store, monkeypatch
):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: (_fake_project(), {}))
    headers, uid = _auth(client, "13900301002")

    a = careerly_store.add_resume_version(uid, "简历 A：3 年 AI 产品", {"skills": ["AI"]})
    first = _analyze(client, headers).json()["data"]
    first_result = first["results"][0]
    assert first_result["resumeVersionId"] == a["id"]
    fingerprint_before = first_result["resumeFingerprint"]

    # 新简历成为当前版本，A 变成「历史版本」
    b = careerly_store.add_resume_version(uid, "简历 B：5 年后端", {"skills": ["Go"]})
    second = _analyze(client, headers).json()["data"]
    assert second["results"][0]["resumeVersionId"] == b["id"]

    # 删除 A
    deleted = client.delete(f"/api/v1/resume/versions/{a['id']}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    remaining_ids = {v["id"] for v in deleted.json()["data"]["versions"]}
    assert a["id"] not in remaining_ids

    # 历史分析仍可追溯到「当时用的是哪一版」—— id / 标签 / 指纹都是快照。
    again = client.get(f"/api/v1/analysis/projects/{first['id']}", headers=headers)
    assert again.status_code == 200, again.text
    result = again.json()["data"]["results"][0]
    assert result["resumeVersionId"] == a["id"]
    assert result["resumeVersionLabel"] == a["label"], \
        "版本行删除后仍必须保留当时的标签快照"
    assert result["resumeFingerprint"] == fingerprint_before, \
        "简历指纹是分析时写下的不可变引用，不得随版本删除变化"

    # 第二次分析（B）不受影响
    second_again = client.get(
        f"/api/v1/analysis/projects/{second['id']}", headers=headers
    )
    assert second_again.json()["data"]["results"][0]["resumeVersionId"] == b["id"]

    # 当前简历仍是 B —— 删除历史版本不得篡改分析基线
    current = client.get("/api/v1/resume", headers=headers).json()["data"]
    assert current["versions"][0]["id"] == b["id"]
