"""P1-1a / P1-2 —— 分析任务状态机与失败语义的回归测试。

覆盖：

1. ``POST /analysis`` 成功后 ``analysis_tasks`` 留下 ``completed`` 记录
   （``projectId`` 已回填、``durationMs`` 非空）
2. ``POST /analysis`` 失败后留下 ``failed`` 记录 + ``errorCode``（消灭「失败无痕」）
3. **P1-2** 缺简历 → ``NO_BASELINE``（不再是语义含糊的 ``NO_RESUME``）
4. **P1-2** RoutingNode 判定 ``ask_user`` → ``NEED_MORE_INPUT``（不是 ANALYSIS_EMPTY）
5. ``GET /analysis/tasks`` 只返回本人任务；``GET /analysis/tasks/{id}`` 越权 → 404
6. 任务端点需要 JWT（401）

设计要点：

* **不跑真实引擎**（一次 200s）。成功/失败路径用 monkeypatch 替换
  ``app.careerly.router.run_analysis``，只验证**任务生命周期的接线**。
  ``ask_user`` 映射则替换 ``orchestrator.DecisionGraph``，验证**映射本身**。
* **不 mock 认证**：用户经真实 ``/auth/register`` 创建，token 是真实 JWT。
* CareerlyStore / AuthStore 指向 ``tmp_path``，不污染 ``app/data/*.db``。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p1_analysis_tasks.py -v
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
from app.core import orchestrator as orchestrator_module
from app.models.job import JobPosting, JobSource
from app.models.match import MatchResult


# ── Fixtures ────────────────────────────────────────────────────────────────
#
# ⚠️ 刻意**不用** pytest 的 ``tmp_path``：它把临时库建在系统 temp 下，会话结束
# 时递归清理，清理数量会触发批量删除保护，整轮测试被沙箱拦下。
#
# ⚠️ 也刻意**不做任何删除**：沙箱的 safe-delete 会拦截 ``unlink``/``rmtree``，
# 在受限路径上失败后直接抛 OSError，把 fixture 打成 ERROR。
#
# 因此这里每个用例开一个 uuid 唯一目录，用完即弃（由 .gitignore 忽略，
# 需要时可整目录手工清理）。零删除 = 零拦截。

_TMP_ROOT = Path(__file__).resolve().parent / ".tmp_p1"


@pytest.fixture()
def workdir() -> Path:
    d = _TMP_ROOT / uuid.uuid4().hex[:12]
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture()
def careerly_store(workdir, monkeypatch) -> CareerlyStore:
    """CareerlyStore 指向临时库，并注入 router 的懒加载单例。"""
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


def _headers(client: TestClient, phone: str, name: str = "测试用户") -> dict[str, str]:
    r = client.post("/api/v1/auth/register", json={"name": name, "phone": phone})
    assert r.status_code == 201, r.text
    return {"Authorization": f"Bearer {r.json()['data']['access_token']}"}


def _fake_project() -> object:
    """一个最小可用的 AnalysisProject（不经 LLM / Embedding）。"""
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


# ── 1. 成功 → completed ──────────────────────────────────────────────────────


def test_success_leaves_a_completed_task(client, careerly_store, monkeypatch):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: _fake_project())
    h = _headers(client, "13900000001")

    r = client.post(
        "/api/v1/analysis",
        json={"title": "高级 Python 后端工程师", "rawJd": "负责 AI 平台后端开发",
              "resumeText": "5 年 Python 经验"},
        headers=h,
    )
    assert r.status_code == 201, r.text
    project_id = r.json()["data"]["id"]

    tasks = client.get("/api/v1/analysis/tasks", headers=h).json()["data"]
    assert len(tasks) == 1, f"应留下 1 条任务记录，实际 {tasks}"

    task = tasks[0]
    assert task["status"] == "completed"
    assert task["projectId"] == project_id, "成功后必须回填 projectId"
    assert task["errorCode"] is None and task["errorMessage"] is None
    assert task["durationMs"] is not None, "completed 任务必须有耗时"
    assert task["createdAt"] and task["startedAt"] and task["finishedAt"]


def test_success_task_is_readable_by_id(client, careerly_store, monkeypatch):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: _fake_project())
    h = _headers(client, "13900000002")

    client.post("/api/v1/analysis",
                json={"title": "T", "rawJd": "D", "resumeText": "R"}, headers=h)
    task_id = client.get("/api/v1/analysis/tasks", headers=h).json()["data"][0]["taskId"]

    one = client.get(f"/api/v1/analysis/tasks/{task_id}", headers=h)
    assert one.status_code == 200
    assert one.json()["data"]["taskId"] == task_id


# ── 2. 失败 → failed + errorCode（消灭「失败无痕」）──────────────────────────


def test_failure_leaves_a_failed_task_with_error_code(client, careerly_store, monkeypatch):
    def boom(**kw):
        raise AnalysisError("NEED_MORE_INPUT", "岗位描述过于简略，无法完成分析", 422)

    monkeypatch.setattr(careerly_router, "run_analysis", boom)
    h = _headers(client, "13900000003")

    r = client.post("/api/v1/analysis",
                    json={"title": "T", "rawJd": "D", "resumeText": "R"}, headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NEED_MORE_INPUT"

    task = client.get("/api/v1/analysis/tasks", headers=h).json()["data"][0]
    assert task["status"] == "failed"
    assert task["errorCode"] == "NEED_MORE_INPUT"
    assert "无法完成分析" in task["errorMessage"]
    assert task["projectId"] is None, "失败任务不得回填 projectId"


def test_unexpected_crash_is_recorded_as_failed(client, careerly_store, monkeypatch):
    def boom(**kw):
        raise RuntimeError("引擎内部炸了")

    monkeypatch.setattr(careerly_router, "run_analysis", boom)
    h = _headers(client, "13900000004")

    r = client.post("/api/v1/analysis",
                    json={"title": "T", "rawJd": "D", "resumeText": "R"}, headers=h)
    assert r.status_code == 502

    task = client.get("/api/v1/analysis/tasks", headers=h).json()["data"][0]
    assert task["status"] == "failed"
    assert task["errorCode"] == "ANALYSIS_FAILED"


# ── 3/4. P1-2 失败语义 ───────────────────────────────────────────────────────


def test_missing_resume_is_no_baseline(client, careerly_store):
    """缺简历 → NO_BASELINE（P1-2 重命名，语义 =「尚未建立职业基线」）。"""
    h = _headers(client, "13900000005")

    r = client.post("/api/v1/analysis",
                    json={"title": "T", "rawJd": "D"}, headers=h)  # 无 resumeText
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NO_BASELINE", \
        "P1-2：缺简历的代码应为 NO_BASELINE，不是 NO_RESUME"

    # 前置拒绝也必须留痕 —— 这是最常见的失败路径，不能无记录
    task = client.get("/api/v1/analysis/tasks", headers=h).json()["data"][0]
    assert task["status"] == "failed"
    assert task["errorCode"] == "NO_BASELINE"
    assert task["startedAt"] is None, "前置拒绝时引擎从未启动"


def test_quota_rejection_is_recorded(client, careerly_store, monkeypatch):
    """配额不足同样留痕：这是撞到免费墙的直接证据，P5 监控需要它。"""
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: _fake_project())
    h = _headers(client, "13900000009")

    for _ in range(careerly_store.FREE_ANALYSIS_QUOTA):
        client.post("/api/v1/analysis",
                    json={"title": "T", "rawJd": "D", "resumeText": "R"}, headers=h)

    r = client.post("/api/v1/analysis",
                    json={"title": "T", "rawJd": "D", "resumeText": "R"}, headers=h)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "QUOTA_EXCEEDED"

    tasks = client.get("/api/v1/analysis/tasks", headers=h).json()["data"]
    assert len(tasks) == 4, "3 次成功 + 1 次配额拒绝，共 4 条"
    assert tasks[0]["errorCode"] == "QUOTA_EXCEEDED"


def test_ask_user_maps_to_need_more_input(client, careerly_store, monkeypatch):
    """RoutingNode 判定 ask_user → NEED_MORE_INPUT，而非 ANALYSIS_EMPTY。"""

    class _State:
        routing_decision = "ask_user"
        routing_reason = "岗位描述缺少技能要求，无法判断匹配度"
        match_results = []
        errors = []
        resume = None
        session_id = "sess_test"

    class _FakeGraph:
        def run_sync(self, **kw):
            return _State()

    monkeypatch.setattr(orchestrator_module, "DecisionGraph", _FakeGraph)
    h = _headers(client, "13900000006")

    r = client.post("/api/v1/analysis",
                    json={"title": "T", "rawJd": "D", "resumeText": "R"}, headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NEED_MORE_INPUT", \
        "ask_user 必须映射为 NEED_MORE_INPUT（可补救），不得混入 ANALYSIS_EMPTY（故障）"
    assert "技能要求" in r.json()["error"]["message"], "应带上 routing_reason"


# ── 5. 归属与鉴权 ────────────────────────────────────────────────────────────


def test_tasks_are_scoped_to_their_owner(client, careerly_store, monkeypatch):
    monkeypatch.setattr(careerly_router, "run_analysis", lambda **kw: _fake_project())
    a = _headers(client, "13900000007", "A")
    b = _headers(client, "13900000008", "B")

    client.post("/api/v1/analysis",
                json={"title": "A 的分析", "rawJd": "D", "resumeText": "R"}, headers=a)
    a_task_id = client.get("/api/v1/analysis/tasks", headers=a).json()["data"][0]["taskId"]

    # B 的列表里不该出现 A 的任务
    b_tasks = client.get("/api/v1/analysis/tasks", headers=b).json()["data"]
    assert b_tasks == [], f"B 不该看到 A 的任务，实际 {b_tasks}"

    # B 按 id 直取 A 的任务 → 404（与「不存在」同码，不泄露存在性）
    r = client.get(f"/api/v1/analysis/tasks/{a_task_id}", headers=b)
    assert r.status_code == 404, f"越权应为 404，实际 {r.status_code}"


def test_task_endpoints_require_auth(client, careerly_store):
    assert client.get("/api/v1/analysis/tasks").status_code == 401
    assert client.get("/api/v1/analysis/tasks/task_whatever").status_code == 401


# ── 6. 状态机本身（不经 HTTP）────────────────────────────────────────────────


def test_state_machine_transitions(careerly_store):
    tid = careerly_store.create_task(7, "T", source="paste")
    assert careerly_store.get_task(7, tid)["status"] == "queued"

    careerly_store.start_task(tid)
    assert careerly_store.get_task(7, tid)["status"] == "processing"
    assert careerly_store.get_task(7, tid)["startedAt"] is not None

    careerly_store.complete_task(tid, "proj_x")
    done = careerly_store.get_task(7, tid)
    assert done["status"] == "completed"
    assert done["projectId"] == "proj_x"

    # 另一个用户的任务不得被读到
    assert careerly_store.get_task(8, tid) is None


def test_task_owner_is_non_nullable(careerly_store):
    """fail-closed 由**表结构**保证，不靠查询纪律。

    ``analysis_tasks.user_id`` 是 NOT NULL —— 与 ``graph_state.db`` 的
    ``sessions``（历史行 user_id 为 NULL）不同，这张表从第一版起就不允许
    无主任务存在。任何绕过 API 层直接写库、试图制造孤儿任务的尝试都会失败。
    """
    import sqlite3

    conn = sqlite3.connect(careerly_store.db_path)
    with pytest.raises(sqlite3.IntegrityError, match="user_id"):
        conn.execute(
            "INSERT INTO analysis_tasks (id, user_id, project_id, status, title, source, "
            "created_at) VALUES ('task_orphan', NULL, NULL, 'queued', 'x', 'paste', 0)"
        )
    conn.close()
