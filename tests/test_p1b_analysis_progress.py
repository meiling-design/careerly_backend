"""P1-1b —— 分析进度可观测面的回归测试（2026-10-05）。

覆盖本次新增的「引擎节点 → 产品步骤」整条链路：

1. ``_NODE_TO_STEP`` 覆盖引擎全部 8 个节点，且值域落在 0..4
2. ``_progress_payload``：无 session / 未知 session → ``None``（不是 0%）
3. ``_progress_payload``：读到进度 → 正确的 ``stepIndex`` / 两个刻度各自取值
4. 未知节点 → ``stepIndex`` 为 ``None``（客户端应保持当前步不动）
5. ``GET /analysis/tasks/{id}`` 带上 ``progress``
6. 未绑定 session 的历史任务 → ``progress`` 为 ``None``
7. **归属**：他人的任务仍 404 —— 进度不能成为绕过任务归属的越权读路径
8. ``POST /analysis/tasks`` → 202 + ``taskId``，且任务行已绑定 session 并进入 processing
9. 异步入口的前置拒绝（无基线）→ 422 **且**留下 failed 任务（失败不无痕）
10. 同步 ``POST /analysis`` 的任务行现在也带 ``sessionId``（双模共用同一套绑定）

设计要点：

* **不跑真实引擎**（一次 200s）。同步路径 monkeypatch ``run_analysis``；
  异步路径只断言 202 时刻**保证成立**的东西 —— 后台任务何时被事件循环
  调起来是调度问题，断言它会导致 flaky。
* **进度不经 HTTP 写入**：直接往引擎 ``StateStore`` 写，再经 HTTP 读。
  这样测的是「读取与映射」，不掺入引擎真实运行。
* CareerlyStore / AuthStore / StateStore 三个库都指向 ``tmp_path`` 之外的
  唯一目录，且**不做任何删除**（理由见 ``test_p1_analysis_tasks.py`` 顶部）。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p1b_analysis_progress.py -v
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
from app.careerly.router import (
    ANALYSIS_STEP_COUNT,
    ENGINE_NODE_COUNT,
    _NODE_TO_STEP,
    _progress_payload,
)
from app.careerly.store import CareerlyStore
from app.core import auth_store as auth_store_module
from app.core import state_store as state_store_module
from app.core.state_store import StateStore
from app.models.job import JobPosting, JobSource
from app.models.match import MatchResult


# ── Fixtures ────────────────────────────────────────────────────────────────

_TMP_ROOT = Path(__file__).resolve().parent / ".tmp_p1b"


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
def engine_store(workdir, monkeypatch) -> StateStore:
    """引擎库（``graph_state.db``）—— 与业务库**分库**，这里单独指向临时目录。"""
    monkeypatch.setattr(state_store_module, "_local", threading.local())
    store = StateStore(workdir / "graph_state.db")
    monkeypatch.setattr(careerly_router, "_engine_store", store, raising=False)
    return store


@pytest.fixture()
def client(workdir, monkeypatch, careerly_store, engine_store) -> TestClient:
    monkeypatch.setattr(auth_store_module, "_local", threading.local())
    monkeypatch.setattr(auth_store_module, "DB_PATH", workdir / "auth.db")
    monkeypatch.setattr(auth_store_module, "DATA_DIR", workdir)
    return TestClient(server_module.app)


def _headers(client: TestClient, phone: str, name: str = "测试用户") -> dict[str, str]:
    r = client.post("/api/v1/auth/register", json={"name": name, "phone": phone})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def _fake_project() -> object:
    job = JobPosting(
        title="高级 Python 后端工程师",
        description="负责 AI 平台后端开发",
        company="测试科技",
        required_skills=["Python", "FastAPI"],
        source=JobSource.MANUAL,
    )
    return build_project(
        title="高级 Python 后端工程师",
        raw_jd="负责 AI 平台后端开发，要求 Python / FastAPI",
        match_results=[MatchResult(job=job, score=0.82, skill_overlap=["Python"])],
    )


# ── 1. 映射表本身 ────────────────────────────────────────────────────────────

#: 引擎建图时登记的 8 个节点（``orchestrator.DecisionGraph``）。
ENGINE_NODES = (
    "crawl", "normalize", "resume_embed", "routing",
    "match", "recrawl", "rank", "report",
)


def test_mapping_covers_every_engine_node():
    missing = [n for n in ENGINE_NODES if n not in _NODE_TO_STEP]
    assert not missing, f"这些引擎节点没有对应的产品步骤：{missing}"
    # 多出来的键说明引擎删了节点而映射没跟上 —— 同样是漂移。
    extra = [n for n in _NODE_TO_STEP if n not in ENGINE_NODES]
    assert not extra, f"映射里有引擎已不存在的节点：{extra}"


def test_engine_node_count_matches_mapping():
    assert ENGINE_NODE_COUNT == len(ENGINE_NODES)


def test_step_index_within_product_range():
    for node, step in _NODE_TO_STEP.items():
        assert 0 <= step < ANALYSIS_STEP_COUNT, f"{node} → {step} 越出 0..{ANALYSIS_STEP_COUNT - 1}"


def test_every_product_step_is_reachable():
    """5 个步骤都得有节点落在上面，否则界面上会有一步永远走不到。"""
    reachable = set(_NODE_TO_STEP.values())
    assert reachable == set(range(ANALYSIS_STEP_COUNT))


# ── 2/3/4. _progress_payload ────────────────────────────────────────────────


def test_progress_payload_is_none_without_session(engine_store):
    assert _progress_payload(None) is None


def test_progress_payload_is_none_for_unknown_session(engine_store):
    """读不到 ≠ 进度为 0。客户端据此显示「排队中」，而不是伪造一个读数。"""
    assert _progress_payload("session-does-not-exist") is None


def test_progress_payload_reports_both_scales(engine_store):
    sid = engine_store.new_session_id()
    engine_store.create_session({}, session_id=sid)
    engine_store.update_session_progress(
        sid, current_node="routing", node_status="running",
    )
    engine_store.update_session_progress(sid, completed_count=3)

    p = _progress_payload(sid)
    assert p is not None
    assert p["currentNode"] == "routing"
    assert p["nodeStatus"] == "running"
    # 引擎刻度
    assert p["completedCount"] == 3
    assert p["totalNodes"] == ENGINE_NODE_COUNT
    # 产品刻度 —— routing 属第 2 步「能力匹配分析」
    assert p["stepIndex"] == 2
    assert p["totalSteps"] == ANALYSIS_STEP_COUNT


def test_progress_payload_unknown_node_has_no_step(engine_store):
    """映射表外的节点 → stepIndex 为 None（客户端保持当前步不动，不回退）。"""
    sid = engine_store.new_session_id()
    engine_store.create_session({}, session_id=sid)
    engine_store.update_session_progress(sid, current_node="some_new_node")

    p = _progress_payload(sid)
    assert p is not None
    assert p["currentNode"] == "some_new_node"
    assert p["stepIndex"] is None


# ── 5/6/7. GET /analysis/tasks/{id} 上的进度 ────────────────────────────────


def _run_one_sync_analysis(client, monkeypatch, headers, resume="5 年 Python 经验") -> dict:
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: (_fake_project(), {}))
    r = client.post(
        "/api/v1/analysis",
        json={"title": "高级 Python 后端工程师", "rawJd": "负责 AI 平台后端开发",
              "resumeText": resume},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_get_task_exposes_real_node_progress(client, engine_store, monkeypatch):
    h = _headers(client, "13900001001")
    _run_one_sync_analysis(client, monkeypatch, h)

    task = client.get("/api/v1/analysis/tasks", headers=h).json()["data"][0]
    sid = task["sessionId"]
    assert sid, "同步入口也必须绑定引擎 session，否则进度无处可查"

    engine_store.create_session({}, session_id=sid)
    engine_store.update_session_progress(sid, current_node="report", node_status="running")

    got = client.get(f"/api/v1/analysis/tasks/{task['taskId']}", headers=h)
    assert got.status_code == 200
    p = got.json()["data"]["progress"]
    assert p is not None, "任务已绑定 session 且有进度，必须报出来"
    assert p["currentNode"] == "report"
    assert p["stepIndex"] == 4  # 生成分析报告


def test_get_task_progress_is_none_for_unbound_task(client, careerly_store, monkeypatch):
    """历史 / 未绑定 session 的任务：progress 为 None，不是崩溃、也不是 0%。"""
    h = _headers(client, "13900001002")
    uid = client.get("/api/v1/auth/me", headers=h).json()["data"]["user"]["id"]
    # 直接建一条**不绑 session** 的任务，模拟迁移前留下的行。
    task_id = careerly_store.create_task(uid, "遗留任务")

    got = client.get(f"/api/v1/analysis/tasks/{task_id}", headers=h)
    assert got.status_code == 200
    assert got.json()["data"]["sessionId"] is None
    assert got.json()["data"]["progress"] is None


def test_task_progress_does_not_leak_across_users(client, engine_store, monkeypatch):
    """A 的任务，B 查 → 404。进度读路径不能绕开任务归属。"""
    h_a = _headers(client, "13900001003", name="甲")
    h_b = _headers(client, "13900001004", name="乙")
    _run_one_sync_analysis(client, monkeypatch, h_a)

    task = client.get("/api/v1/analysis/tasks", headers=h_a).json()["data"][0]
    sid = task["sessionId"]
    engine_store.create_session({}, session_id=sid)
    engine_store.update_session_progress(sid, current_node="match", node_status="running")

    r = client.get(f"/api/v1/analysis/tasks/{task['taskId']}", headers=h_b)
    assert r.status_code == 404, "越权必须与「不存在」同码，且不泄露进度"


# ── 8/9. POST /analysis/tasks（异步入口）────────────────────────────────────


def test_async_entry_returns_202_with_a_bound_task(client, careerly_store):
    h = _headers(client, "13900001005")
    uid = client.get("/api/v1/auth/me", headers=h).json()["data"]["user"]["id"]
    r = client.post(
        "/api/v1/analysis/tasks",
        json={"title": "后端工程师", "rawJd": "负责后端开发", "resumeText": "5 年经验"},
        headers=h,
    )
    assert r.status_code == 202, r.text
    data = r.json()["data"]
    assert data["taskId"]
    assert data["status"] == "processing"

    # 任务行必须已经绑好 session —— 这是「运行期间能查进度」的前提。
    row = careerly_store.get_task(uid, data["taskId"])
    assert row is not None
    assert row["sessionId"], "异步入口的任务必须预先绑定 session"


def test_async_entry_rejects_without_baseline_and_leaves_a_trace(client, careerly_store):
    """无基线 → 422，**且**留 failed 任务（P1-2「失败不无痕」在异步入口同样成立）。"""
    h = _headers(client, "13900001006")
    r = client.post(
        "/api/v1/analysis/tasks",
        json={"title": "后端工程师", "rawJd": "负责后端开发"},
        headers=h,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "NO_BASELINE"

    tasks = client.get("/api/v1/analysis/tasks", headers=h).json()["data"]
    assert len(tasks) == 1, "前置拒绝也必须留下记录"
    assert tasks[0]["status"] == "failed"
    assert tasks[0]["errorCode"] == "NO_BASELINE"


def test_async_entry_requires_auth(client):
    r = client.post("/api/v1/analysis/tasks", json={"title": "T", "rawJd": "D"})
    assert r.status_code == 401
