"""P0-4 / P0-5 —— 用户隔离与鉴权边界的回归测试（红→绿的安全网）。

覆盖的验收点（对应《P0 Implementation Task》§十 / §十四 / §十五）：

1. 受保护端点无 token → **401**
2. User B 持 User A 的 task_id / session_id → **404**（不是 200、不是 403）
3. 列表端点（``/tasks``、``/sessions``）**只返回自己的** session
4. 历史遗留的 ``user_id IS NULL`` session 对**任何**用户都不可见（fail-closed）
5. ``POST /match`` 显式传入他人 ``session_id`` → 404（防借「续跑」语义越权）
6. 幂等键按 user 作用域，A/B 提交相同输入不会互相拿到 session
7. ``PermissionDenied`` 映射为 404 而非 403（决策 D-G：不泄露资源存在性）

设计要点：**不 mock 认证**。用户经真实 ``/auth/register`` + ``/auth/login`` 创建，
token 是真实 JWT —— 这样 P0-3（Auth 链路）与 P0-4（归属）一起被验证。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p0_user_isolation.py -v
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.api import router_app, router_v1
from app.api import server as server_module
from app.core import auth_store as auth_store_module
from app.core import state_store as state_store_module
from app.core.errors import PermissionDenied, ResourceNotFound
from app.core.state_store import StateStore


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch) -> StateStore:
    """把 StateStore 指向临时库，并清掉线程级连接缓存。

    ``_get_conn`` 只在「该线程还没有连接」时才使用传入的 db_path，
    所以必须换掉 ``_local``，否则上一个测试的连接会被复用（指向旧临时库）。
    """
    monkeypatch.setattr(state_store_module, "_local", threading.local())
    monkeypatch.setattr(state_store_module, "DB_PATH", tmp_path / "graph_state.db")
    store = StateStore(tmp_path / "graph_state.db")
    for module in (router_app, router_v1, server_module):
        monkeypatch.setattr(module, "_store", store, raising=False)
    return store


@pytest.fixture()
def client(tmp_path, monkeypatch, isolated_store) -> TestClient:
    """TestClient，auth.db 同样指向临时库。"""
    monkeypatch.setattr(auth_store_module, "_local", threading.local())
    monkeypatch.setattr(auth_store_module, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setattr(auth_store_module, "DATA_DIR", tmp_path)
    return TestClient(server_module.app)


def _register_and_login(client: TestClient, phone: str, name: str) -> dict[str, str]:
    """真实注册 + 登录，返回可直接使用的 Authorization 头。"""
    r = client.post("/api/v1/auth/register", json={"name": name, "phone": phone})
    assert r.status_code == 201, r.text
    payload = r.json()["data"]
    assert payload["access_token"], "注册应返回 token 对"
    return {"Authorization": f"Bearer {payload['access_token']}"}


# ── 1. 未认证 → 401 ─────────────────────────────────────────────────────────


PROTECTED_GETS = [
    "/api/v1/tasks",
    "/api/v1/tasks/whatever",
    "/api/v1/tasks/whatever/result",
    "/api/v1/sessions",
    "/api/v1/sessions/whatever",
    "/api/v1/sessions/whatever/report",
    "/sessions",
    "/sessions/whatever",
    "/sessions/whatever/report",
]


@pytest.mark.parametrize("path", PROTECTED_GETS)
def test_protected_endpoints_require_token(client: TestClient, path: str) -> None:
    """没有 Bearer token 一律 401 —— 不再有「未登录即可读他人分析结果」的裸奔端点。"""
    assert client.get(path).status_code == 401, path


def test_resume_upload_requires_token(client: TestClient) -> None:
    """POST /resume/upload 曾完全无鉴权：它会产出属于用户的数据，必须认证。"""
    files = {"file": ("r.txt", b"hello", "text/plain")}
    assert client.post("/api/v1/resume/upload", files=files).status_code == 401


# ── 2 & 3. 归属隔离 ─────────────────────────────────────────────────────────


def test_user_b_cannot_read_user_a_task_or_session(client: TestClient, isolated_store: StateStore) -> None:
    """核心安全验收：B 拿到 A 的 id 也读不到任何东西（404，非 403）。"""
    a_headers = _register_and_login(client, "13800000001", "User A")
    b_headers = _register_and_login(client, "13800000002", "User B")

    a_session = isolated_store.create_session({"job_urls": 1}, user_id=1)

    # A 能读自己的
    assert client.get(f"/api/v1/tasks/{a_session}", headers=a_headers).status_code == 200
    assert client.get(f"/api/v1/sessions/{a_session}", headers=a_headers).status_code == 200

    # B 读同一个 id → 404（不是 200，也**不是** 403）
    for path in (
        f"/api/v1/tasks/{a_session}",
        f"/api/v1/tasks/{a_session}/result",
        f"/api/v1/sessions/{a_session}",
        f"/api/v1/sessions/{a_session}/report",
    ):
        resp = client.get(path, headers=b_headers)
        assert resp.status_code == 404, f"{path} 应返回 404，实际 {resp.status_code}"
        assert resp.json()["error"]["code"] == "NOT_FOUND"


def test_list_endpoints_only_return_own_sessions(client: TestClient, isolated_store: StateStore) -> None:
    """列表端点必须按 user 过滤，否则「我的分析」会混进别人的。"""
    a_headers = _register_and_login(client, "13800000011", "User A")
    b_headers = _register_and_login(client, "13800000012", "User B")

    a_session = isolated_store.create_session({"job_urls": 3}, user_id=1)
    b_session = isolated_store.create_session({"job_urls": 3}, user_id=2)

    # (路径, 信封里的集合键, 条目里的 id 字段)
    listings = (
        ("/api/v1/tasks", "tasks", "task_id"),
        ("/api/v1/sessions", "sessions", "session_id"),
    )
    for list_path, collection_key, id_field in listings:
        data = client.get(list_path, headers=a_headers).json()["data"][collection_key]
        assert {item[id_field] for item in data} == {a_session}, f"{list_path} 应只含 A 的 session"

        data_b = client.get(list_path, headers=b_headers).json()["data"][collection_key]
        assert {item[id_field] for item in data_b} == {b_session}, f"{list_path} 应只含 B 的 session"


# ── 4. 历史遗留 session（user_id 为 NULL）────────────────────────────────────


def test_legacy_unowned_session_is_invisible_to_everyone(client: TestClient, isolated_store: StateStore) -> None:
    """P0-4 的旧数据纪律：NULL 归属必须 fail-closed。

    ``WHERE user_id = ?`` 永不匹配 NULL，所以 ALTER TABLE 之后历史行自动对所有
    用户不可见 —— 无需清理脚本，但**禁止**把查询退化成 ``OR user_id IS NULL``。
    """
    headers = _register_and_login(client, "13800000021", "User A")
    legacy = isolated_store.create_session({"job_urls": 1})  # user_id 不传 → NULL

    assert isolated_store.get_session_info(legacy, user_id=1) is None
    assert client.get(f"/api/v1/tasks/{legacy}", headers=headers).status_code == 404
    assert client.get(f"/api/v1/sessions/{legacy}", headers=headers).status_code == 404

    listed = client.get("/api/v1/tasks", headers=headers).json()["data"]["tasks"]
    assert legacy not in {t["task_id"] for t in listed}


# ── 5. 显式传入他人 session_id ──────────────────────────────────────────────


def test_submit_match_rejects_foreign_session_id(client: TestClient, isolated_store: StateStore) -> None:
    """``POST /match`` 允许客户端续跑（传 session_id），但不得续跑**别人的**。"""
    a_headers = _register_and_login(client, "13800000031", "User A")
    b_headers = _register_and_login(client, "13800000032", "User B")

    a_session = isolated_store.create_session({"job_urls": 1}, user_id=1)

    resp = client.post(
        "/api/v1/match",
        headers=b_headers,
        json={
            "resume_text": "5年 Python 经验",
            "job_urls": ["https://example.com/jobs/1"],
            "session_id": a_session,
        },
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "NOT_FOUND"


# ── 6. 幂等键作用域 ─────────────────────────────────────────────────────────


def test_idempotency_key_is_scoped_per_user(isolated_store: StateStore) -> None:
    """A/B 提交**完全相同**的输入时，幂等键相同 —— 不能因此互认 session。"""
    a_session = isolated_store.create_session({"idempotency_key": "same-key"}, user_id=1)

    assert isolated_store.find_by_idempotency_key("same-key", user_id=1) == {
        "session_id": a_session,
        "status": "pending",
    }
    assert isolated_store.find_by_idempotency_key("same-key", user_id=2) is None


# ── 7. 决策 D-G：404 而非 403 ───────────────────────────────────────────────


def test_permission_denied_is_indistinguishable_from_not_found() -> None:
    """越权与不存在必须返回同一个状态码与错误码 —— 否则可枚举他人资源。"""
    assert PermissionDenied.status_code == ResourceNotFound.status_code == 404
    assert PermissionDenied.code == ResourceNotFound.code == "NOT_FOUND"


def test_app_error_handler_emits_unified_envelope(client: TestClient) -> None:
    """AppError 走统一信封（回归保护：别退化成 FastAPI 默认的 {"detail": ...}）。"""
    from app.core.errors import AppError

    @server_module.app.get("/__p0_test_permission_denied", include_in_schema=False)
    async def _boom():  # pragma: no cover - 测试专用路由
        raise PermissionDenied("nope")

    try:
        resp = client.get("/__p0_test_permission_denied")
        assert resp.status_code == 404
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "NOT_FOUND"
        assert "meta" in body and "timestamp" in body["meta"]
        assert isinstance(AppError("x"), Exception)
    finally:
        server_module.app.router.routes = [
            r for r in server_module.app.router.routes
            if getattr(r, "path", None) != "/__p0_test_permission_denied"
        ]
