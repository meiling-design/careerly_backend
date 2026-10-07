"""发布基线 —— 生产环境硬阻断 + 令牌吊销（K24 / K21 / 决策 D119）。

被保护的不变量
--------------
```text
JWT 密钥    ENV=production 且密钥缺失/默认/过短  →  拒绝启动
CORS        ENV=production 且未显式配白名单      →  拒绝启动
吊销        账号注销后，其 access 与 refresh token 立刻失效
注销不可绕   注销后重新用密码登录，不得拿到可用凭据
```

为什么这些必须被测试钉住
------------------------
三条硬阻断都是**导入期**行为，而导入期代码最容易在某次重构中被降级成
一句 warning —— 那种改动不会让任何既有测试变红，只会在生产静默生效。

吊销那条的退化方式更隐蔽：只要**漏掉 refresh** 的比对，access 60 分钟就过期
（看起来「差不多生效了」），而 90 天有效期的 refresh 仍可无限换新。
所以下面 access 与 refresh **各测一遍**。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_release_security_baseline.py -v
"""

from __future__ import annotations

import contextlib
import importlib
import threading
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api import router_app, router_v1
from app.api import router_auth as router_auth_module
from app.api import server as server_module
from app.core import account_purge
from app.core import auth_store as auth_store_module
from app.core import security as security_module
from app.core import state_store as state_store_module
from app.core.auth_store import AuthStore
from app.core.security import InsecureSecretConfiguration
from app.core.state_store import StateStore

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
REFRESH = "/api/v1/auth/refresh"
ACCOUNT = "/api/v1/account"
CAREER_DATA = "/api/v1/account/career-data"

_GOOD_SECRET = "S" * 48

# 所有安全相关的环境变量 —— 每个用例都从「全部未设置」开始。
_SECURITY_ENV_KEYS = ("ENV", "APP_ENV", "AUTH_MODE", "JWT_SECRET_KEY", "CORS_ALLOW_ORIGINS")


def _clear_security_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _SECURITY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@contextlib.contextmanager
def _env(monkeypatch: pytest.MonkeyPatch, **values: str) -> Iterator[None]:
    """Set env for an import-time check, then restore the module to dev state.

    ``importlib.reload`` 失败时会留下**半初始化的模块字典**，所以无论成功
    与否都在 finally 里用干净环境重新加载一次 —— 否则后面的用例会读到
    上一个用例的密钥配置。
    """
    _clear_security_env(monkeypatch)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    try:
        yield
    finally:
        _clear_security_env(monkeypatch)
        importlib.reload(security_module)


# ── JWT 密钥硬阻断 ──────────────────────────────────────────────────────────


def test_production_refuses_default_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥留空 = 用默认值 = 任何人都能伪造任意账号的 token。"""
    with _env(monkeypatch, ENV="production", AUTH_MODE="formal"):
        with pytest.raises(InsecureSecretConfiguration):
            importlib.reload(security_module)


def test_production_refuses_short_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """薄弱密钥与缺失密钥同样致命 —— HS256 的强度就是它的熵。"""
    with _env(monkeypatch, ENV="production", AUTH_MODE="formal", JWT_SECRET_KEY="tooshort"):
        with pytest.raises(InsecureSecretConfiguration):
            importlib.reload(security_module)


def test_production_accepts_long_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """配齐之后必须能正常启动 —— 否则这条防护会被「先关掉再说」。"""
    with _env(monkeypatch, ENV="production", AUTH_MODE="formal", JWT_SECRET_KEY=_GOOD_SECRET):
        module = importlib.reload(security_module)
        assert module.SECRET_KEY == _GOOD_SECRET


def test_development_allows_default_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """本地零配置必须仍然可跑，否则没人愿意用它开发。"""
    with _env(monkeypatch, ENV="development"):
        module = importlib.reload(security_module)
        assert module.SECRET_KEY == "dev-insecure-change-me"


# ── CORS 硬阻断 ─────────────────────────────────────────────────────────────


def test_production_refuses_wildcard_cors(monkeypatch: pytest.MonkeyPatch) -> None:
    """未配置时回退到 '*'，等于任何站点都能从用户浏览器调用本 API。"""
    with _env(
        monkeypatch,
        ENV="production",
        AUTH_MODE="formal",
        JWT_SECRET_KEY=_GOOD_SECRET,
    ):
        with pytest.raises(server_module.InsecureCorsConfiguration):
            importlib.reload(server_module)


def test_production_accepts_explicit_cors(monkeypatch: pytest.MonkeyPatch) -> None:
    with _env(
        monkeypatch,
        ENV="production",
        AUTH_MODE="formal",
        JWT_SECRET_KEY=_GOOD_SECRET,
        CORS_ALLOW_ORIGINS="https://app.example.com",
    ):
        importlib.reload(server_module)  # 不抛即通过
    importlib.reload(server_module)


# ── 令牌吊销（决策 D119）────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """TestClient 指向临时库（沿用 tests/test_auth_password.py 的隔离方式）。"""
    monkeypatch.setattr(state_store_module, "_local", threading.local())
    monkeypatch.setattr(state_store_module, "DB_PATH", tmp_path / "graph_state.db")
    store = StateStore(tmp_path / "graph_state.db")
    for module in (router_app, router_v1, server_module):
        monkeypatch.setattr(module, "_store", store, raising=False)

    monkeypatch.setattr(auth_store_module, "_local", threading.local())
    monkeypatch.setattr(auth_store_module, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setattr(auth_store_module, "DATA_DIR", tmp_path)

    return TestClient(server_module.app)


def _register(client: TestClient, phone: str = "13800138000") -> dict:
    resp = client.post(
        REGISTER,
        json={"name": "测试用户", "phone": phone, "password": "pw123456"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_soft_delete_revokes_access_token(client: TestClient) -> None:
    data = _register(client)
    access = data["access_token"]
    user_id = data["user"]["id"]

    assert client.get(ME, headers=_auth(access)).status_code == 200

    assert AuthStore().soft_delete_user(user_id) is True

    resp = client.get(ME, headers=_auth(access))
    assert resp.status_code == 401, "注销后 access token 必须立即失效"


def test_soft_delete_revokes_refresh_token(client: TestClient) -> None:
    """⚠️ 最容易漏的一条：只比对 access、不比对 refresh，吊销就形同虚设。

    access 只有 60 分钟，漏检看起来「差不多生效」；但 refresh 有 90 天，
    且每次使用都会轮换出新的一对 —— 不拦住它，注销后的账号能被无限续期。
    """
    data = _register(client)
    refresh = data["refresh_token"]
    user_id = data["user"]["id"]

    assert client.post(REFRESH, json={"refresh_token": refresh}).status_code == 200

    assert AuthStore().soft_delete_user(user_id) is True

    resp = client.post(REFRESH, json={"refresh_token": refresh})
    assert resp.status_code == 401, "注销后 refresh token 必须立即失效"


def test_deleted_account_cannot_log_back_in(client: TestClient) -> None:
    """注销后密码仍然有效 —— 少了 status 检查，用户重新登录即可换到新版本
    号的 token，吊销被完全绕过。"""
    data = _register(client)
    AuthStore().soft_delete_user(data["user"]["id"])

    resp = client.post(LOGIN, json={"phone": "13800138000", "password": "pw123456"})
    assert resp.status_code in (401, 403), resp.text


def test_soft_delete_is_idempotent(client: TestClient) -> None:
    """重复注销不应把 token_version 一直往上加。"""
    data = _register(client)
    user_id = data["user"]["id"]
    store = AuthStore()

    assert store.soft_delete_user(user_id) is True
    assert store.soft_delete_user(user_id) is False

    version = store.get_by_id(user_id)["token_version"]
    assert version == 1


def test_token_version_survives_reissue(client: TestClient) -> None:
    """吊销后重新签发的 token 带新版本号，必须能正常使用 ——
    否则「重新登录」会变成一个坏掉的状态。"""
    data = _register(client)
    user_id = data["user"]["id"]

    AuthStore().bump_token_version(user_id)

    resp = client.post(LOGIN, json={"phone": "13800138000", "password": "pw123456"})
    assert resp.status_code == 200, resp.text
    fresh = resp.json()["data"]["access_token"]
    assert client.get(ME, headers=_auth(fresh)).status_code == 200


def test_legacy_token_without_version_claim_still_works(client: TestClient) -> None:
    """存量 token 没有 token_version 声明 —— 按 0 处理，不因上线被集体登出。"""
    data = _register(client)
    user = data["user"]
    legacy = security_module.create_access_token(str(user["id"]))  # 默认 version=0
    assert client.get(ME, headers=_auth(legacy)).status_code == 200


# ── DELETE /api/v1/account（决策 D119）────────────────────────────────────


def test_delete_account_requires_auth(client: TestClient) -> None:
    """不带凭据不能注销任何人的账号。"""
    assert client.delete(ACCOUNT).status_code == 401


def test_delete_career_data_requires_auth(client: TestClient) -> None:
    assert client.delete(CAREER_DATA).status_code == 401


def test_delete_career_data_preserves_account_and_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清理职业数据不应注销账号、吊销 token 或跳过级联清理器。"""
    data = _register(client)
    user_id = data["user"]["id"]
    token = data["access_token"]
    before_version = AuthStore().get_by_id(user_id)["token_version"]
    calls: list[tuple[int, bool]] = []

    def fake_purge(target_user_id: int, *, dry_run: bool):
        calls.append((target_user_id, dry_run))
        return account_purge.PurgeReport(
            user_id=target_user_id,
            dry_run=dry_run,
            rows={"careerly.db:careerly_resumes": 2},
            files={"files:data/resumes": 1},
        )

    monkeypatch.setattr(router_auth_module.account_purge, "purge_user", fake_purge)
    response = client.delete(CAREER_DATA, headers=_auth(token))

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["deleted"] is True
    assert payload["account_retained"] is True
    assert payload["deleted_rows"] == 2
    assert payload["deleted_files"] == 1
    assert payload["orphan_artifacts"] == 0
    assert payload["warnings"] == []
    assert calls == [(user_id, False)]
    assert AuthStore().get_by_id(user_id)["token_version"] == before_version
    assert client.get(ME, headers=_auth(token)).status_code == 200


def test_delete_career_data_reports_incomplete_file_cleanup(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _register(client)
    monkeypatch.setattr(
        router_auth_module.account_purge,
        "purge_user",
        lambda user_id, *, dry_run: account_purge.PurgeReport(
            user_id=user_id,
            dry_run=dry_run,
            orphan_artifacts=2,
            warnings=["2 个产物文件无法归属到用户"],
        ),
    )

    response = client.delete(CAREER_DATA, headers=_auth(data["access_token"]))

    assert response.status_code == 200, response.text
    assert response.json()["data"]["warnings"] == [
        "部分历史文件无法确认归属，未被删除。"
    ]
    assert response.json()["data"]["orphan_artifacts"] == 2


def test_delete_account_revokes_everything(client: TestClient) -> None:
    """一次调用之后：access 失效、refresh 失效、无法重新登录。

    这三条必须一起成立 —— 「删除账号」对用户而言是**一个动作**，
    任何一条漏掉都会让它变成「看起来删掉了」。
    """
    data = _register(client)
    access, refresh = data["access_token"], data["refresh_token"]

    resp = client.delete(ACCOUNT, headers=_auth(access))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["deleted"] is True

    assert client.get(ME, headers=_auth(access)).status_code == 401
    assert client.post(REFRESH, json={"refresh_token": refresh}).status_code == 401
    login = client.post(LOGIN, json={"phone": "13800138000", "password": "pw123456"})
    assert login.status_code in (401, 403), login.text


def test_second_delete_with_same_token_is_401_not_409(client: TestClient) -> None:
    """第二次用同一个 token 注销 → 401，而不是 409。

    端点里那条 409（``ACCOUNT_ALREADY_DELETED``）**走不到这条路径**：
    第一次注销已经 ``token_version += 1``，而 ``get_current_user`` 在进入
    端点之前就会因 status / 版本不符回 401。409 真正防的是**并发** ——
    两个请求同时通过鉴权、其中一个先提交，另一个才会落到那一支。
    这里断言的是用户实际会看到的形态（401），避免把 409 当成可达行为。
    """
    data = _register(client)
    token = data["access_token"]
    assert client.delete(ACCOUNT, headers=_auth(token)).status_code == 200
    assert client.delete(ACCOUNT, headers=_auth(token)).status_code == 401


def test_delete_account_reports_grace_period(client: TestClient) -> None:
    """响应要告诉客户端缓冲期 —— 前端据此展示「30 天后彻底删除」。"""
    data = _register(client)
    payload = client.delete(ACCOUNT, headers=_auth(data["access_token"])).json()["data"]
    assert payload["purge_after_days"] == 30
    assert payload["purge_after_ts"] > 0
