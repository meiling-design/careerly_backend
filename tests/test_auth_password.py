"""密码认证与第三方登录接缝的回归测试（2026-10-04 裁决）。

背景
----
新版 Figma 设计把「密码」放进了注册页 / 登录页，并增加了「微信 / QQ」按钮。
产品侧据此裁决：

    1. 密码**端到端接通**（bcrypt，最保守规则：非空且 >= 6 位）
    2. 第三方登录**只建可配置契约** —— 未配置凭据时返回 501 + 明确原因

本文件钉住的不变量
------------------
```text
身份分层      有 password_hash 的账号 = 真实凭据，登录必须带对密码
              无 password_hash 的账号 = 遗留 / DEV，仍受 AUTH_MODE 门禁
哈希不外泄    /auth/register、/auth/login、/auth/me 的响应体里不得出现哈希
接缝可识别    未配置凭据 → OAUTH_NOT_CONFIGURED(501)，
              配置齐了但未实现 → OAUTH_UNAVAILABLE(501)，
              渠道不认识 → OAUTH_PROVIDER_UNSUPPORTED(404)
```

⚠️ 最容易退化的一条是「**密码账号不受 AUTH_MODE 门禁**」——
写错的实现会把真实凭据校验也塞进 ``is_dev_auth()`` 分支，于是
``AUTH_MODE=formal`` 的生产部署下**谁都登录不了**。本文件显式断言其反面。

另一个容易退化的点是 ``password`` 在请求模型上是**选填**的
（见 ``app/api/router_auth.py`` 模块文档）—— 这是为了不打断 6 个既有
后端测试里「仅 name + phone 注册」的调用。下面用「无密码注册仍成功」
把这条刻意的宽容钉住，避免有人「顺手」把它改成必填。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_auth_password.py -v
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.api import router_app, router_v1
from app.api import server as server_module
from app.core import auth_store as auth_store_module
from app.core import state_store as state_store_module
from app.core.state_store import StateStore

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"
OAUTH = "/api/v1/auth/oauth/{provider}"


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """TestClient 指向临时库；两个 store 的线程级连接缓存都要清掉。

    ``_get_conn`` 只在「该线程还没有连接」时才使用传入的 db_path ——
    不清 ``_local`` 就会复用上一个测试的连接（指向旧临时库）。
    """
    monkeypatch.setattr(state_store_module, "_local", threading.local())
    monkeypatch.setattr(state_store_module, "DB_PATH", tmp_path / "graph_state.db")
    store = StateStore(tmp_path / "graph_state.db")
    for module in (router_app, router_v1, server_module):
        monkeypatch.setattr(module, "_store", store, raising=False)

    monkeypatch.setattr(auth_store_module, "_local", threading.local())
    monkeypatch.setattr(auth_store_module, "DB_PATH", tmp_path / "auth.db")
    monkeypatch.setattr(auth_store_module, "DATA_DIR", tmp_path)

    # OAuth 凭据是环境变量 —— 清干净，让「未配置」成为默认情形。
    for key in (
        "CAREERLY_OAUTH_WECHAT_APP_ID",
        "CAREERLY_OAUTH_WECHAT_APP_SECRET",
        "CAREERLY_OAUTH_QQ_APP_ID",
        "CAREERLY_OAUTH_QQ_APP_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)

    return TestClient(server_module.app)


def _register(client: TestClient, phone: str, name: str, password: str | None = None) -> dict:
    body: dict = {"name": name, "phone": phone}
    if password is not None:
        body["password"] = password
    return client.post(REGISTER, json=body)


# ── 1. 密码注册 → 密码登录 ──────────────────────────────────────────────────


def test_register_with_password_then_login(client: TestClient) -> None:
    """主链路：带密码注册后用同一密码登录，拿到可用 token。"""
    r = _register(client, "13800000001", "张三", "secret66")
    assert r.status_code == 201, r.text
    assert r.json()["success"] is True

    r = client.post(LOGIN, json={"phone": "13800000001", "password": "secret66"})
    assert r.status_code == 200, r.text
    payload = r.json()["data"]
    assert payload["access_token"]

    # token 真的可用（不是「签了个空串」）
    r = client.get(ME, headers={"Authorization": f"Bearer {payload['access_token']}"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["user"]["phone"] == "13800000001"


def test_password_is_stored_as_bcrypt_hash(client: TestClient) -> None:
    """落库的是 bcrypt 哈希，不是明文 —— 直接查表断言。"""
    _register(client, "13800000002", "李四", "secret66")

    from app.core.auth_store import AuthStore

    store = AuthStore()
    user = store.get_by_phone("13800000002")
    assert user is not None
    stored = store.get_password_hash(user["id"])

    assert stored is not None
    assert stored != "secret66", "密码不得明文入库"
    assert stored.startswith("$2"), f"应为 bcrypt 格式，实际：{stored[:8]}"


# ── 2. 密码不对 / 不给密码 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        {"phone": "13800000003", "password": "wrongpass"},
        {"phone": "13800000003"},  # 完全不给
        {"phone": "13800000003", "password": ""},  # 空串
    ],
    ids=["wrong", "missing", "empty"],
)
def test_login_fails_without_correct_password(client: TestClient, body: dict) -> None:
    """有密码的账号：密码错 / 缺 / 空，一律 401（不是 500，也不是 200）。"""
    _register(client, "13800000003", "王五", "secret66")

    r = client.post(LOGIN, json=body)
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_short_password_on_login_is_401_not_422(client: TestClient) -> None:
    """短密码属于「密码不对」，不是「格式不对」。

    ``LoginRequest.password`` 刻意不设 min_length：若返回 422，
    等于把「密码错误」与「格式不合法」区分开，对撞库者有额外信息量。
    """
    _register(client, "13800000004", "赵六", "secret66")

    r = client.post(LOGIN, json={"phone": "13800000004", "password": "x"})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"


def test_register_rejects_too_short_password(client: TestClient) -> None:
    """注册端相反：显式给了密码就必须满足最小长度（否则 422）。"""
    r = _register(client, "13800000005", "钱七", "12345")
    assert r.status_code == 422, r.text


def test_unregistered_phone_still_reports_phone_not_registered(client: TestClient) -> None:
    """未注册手机号保持既有语义（未决项 C1b，本次不改）。"""
    r = client.post(LOGIN, json={"phone": "13999999999", "password": "secret66"})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "PHONE_NOT_REGISTERED"


def test_hash_never_appears_in_any_response(client: TestClient) -> None:
    """三个响应体里都不得出现哈希字段（``_row_to_user`` 的硬约束）。"""
    r = _register(client, "13800000006", "孙八", "secret66")
    assert "password_hash" not in r.text
    assert "secret66" not in r.text

    r = client.post(LOGIN, json={"phone": "13800000006", "password": "secret66"})
    assert "password_hash" not in r.text
    assert "secret66" not in r.text

    token = r.json()["data"]["access_token"]
    r = client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert "password_hash" not in r.text
    assert "secret66" not in r.text


# ── 3. 无密码账号：遗留 / DEV 路径 ─────────────────────────────────────────


def test_passwordless_account_is_still_accepted_under_dev(client: TestClient) -> None:
    """**刻意保留的宽容**：只带 name + phone 注册仍然成功。

    6 个既有后端测试文件用这种方式建用户（见 ``router_auth.py`` 模块文档）。
    如果有人「顺手」把 ``password`` 改成必填，这条会红。
    """
    r = _register(client, "13800000007", "周九")
    assert r.status_code == 201, r.text

    r = client.post(LOGIN, json={"phone": "13800000007"})
    assert r.status_code == 200, r.text


def test_passwordless_account_is_blocked_when_dev_auth_is_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无密码账号在生产形态下必须被拦住 —— 否则「知道手机号即可接管」。"""
    _register(client, "13800000008", "吴十")

    monkeypatch.setenv("AUTH_MODE", "formal")
    r = client.post(LOGIN, json={"phone": "13800000008"})
    assert r.status_code == 501, r.text
    assert r.json()["error"]["code"] == "AUTH_MODE_UNAVAILABLE"


def test_formal_mode_does_not_reveal_whether_a_phone_is_registered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P0-6 的反枚举性质：非 DEV 形态下探测任意手机号，答案必须一致。

    原实现把 ``is_dev_auth()`` 放在**最前面**，所以正式形态下
    「已注册」与「未注册」都得到同一个 501。
    本次改动为了让密码账号能在正式形态下登录，必须把账户查出来才能
    判断它有没有密码 —— 这个「先查库」的重构极容易顺手把
    ``if user is None: 401`` 提到门禁之前，于是同一部署下
    「未注册」变成 401、「已注册」变成 501，**手机号是否注册就被探测出来了**。
    这条断言就是钉住那个顺序的。
    """
    # 一个真实存在的无密码账号
    _register(client, "13800000010", "冯十二")
    monkeypatch.setenv("AUTH_MODE", "formal")

    registered = client.post(LOGIN, json={"phone": "13800000010"})
    unregistered = client.post(LOGIN, json={"phone": "13900000000"})

    assert registered.status_code == 501, registered.text
    assert unregistered.status_code == 501, unregistered.text
    assert (
        registered.json()["error"]["code"]
        == unregistered.json()["error"]["code"]
        == "AUTH_MODE_UNAVAILABLE"
    )


def _deactivate(phone: str) -> None:
    """把账号置为停用。

    没有对应 API（停用是运维动作），所以测试直接改库 —— 借
    ``AuthStore`` 自己解析出的 db_path，避免在测试里重复拼临时路径。
    """
    from app.core import auth_store as auth_store_module
    from app.core.auth_store import AuthStore

    store = AuthStore()
    conn = auth_store_module._get_conn(store.db_path)
    conn.execute("UPDATE users SET is_active = 0 WHERE phone = ?", (phone,))
    conn.commit()


def test_disabled_password_account_needs_the_right_password_to_learn_it_is_disabled(
    client: TestClient,
) -> None:
    """停用状态放在密码校验之后 —— 没带对密码的人问不出「这个号被停用了」。

    顺序反了的话，任何人拿一个手机号随便试一下就能得到 403，
    从而确认「该号存在且被停用」。
    """
    _register(client, "13800000011", "陈十三", "secret66")
    _deactivate("13800000011")

    # 密码错 → 401（与「密码不对」不可区分），不暴露停用状态
    r = client.post(LOGIN, json={"phone": "13800000011", "password": "nope123"})
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "INVALID_CREDENTIALS"

    # 密码对 → 才告诉账号本人「已停用」
    r = client.post(LOGIN, json={"phone": "13800000011", "password": "secret66"})
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "ACCOUNT_DISABLED"


def test_password_account_works_even_when_dev_auth_is_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ 本文件最重要的一条：密码是**真实凭据**，不该被 DEV 门禁连坐。

    写错的实现会把整个登录塞进 ``if not is_dev_auth(): 501`` 分支，
    于是 ``AUTH_MODE=formal`` 的生产部署下**所有用户都登录不了**。
    """
    _register(client, "13800000009", "郑十一", "secret66")
    monkeypatch.setenv("AUTH_MODE", "formal")

    r = client.post(LOGIN, json={"phone": "13800000009", "password": "secret66"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["access_token"]

    # 同一个部署形态下，密码错的仍然拒绝（不是「formal 就放行」）
    r = client.post(LOGIN, json={"phone": "13800000009", "password": "nope123"})
    assert r.status_code == 401, r.text


# ── 4. 第三方登录：可配置契约 ──────────────────────────────────────────────


@pytest.mark.parametrize("provider", ["wechat", "qq"])
def test_oauth_unconfigured_returns_501_with_reason(client: TestClient, provider: str) -> None:
    """未配置凭据 → 501 + 「尚未配置」。

    这是产品侧明确要求的行为：按钮**真实调用**后端并**如实报错**，
    而不是静默失败、也不是假装成功。
    """
    r = client.post(OAUTH.format(provider=provider), json={"code": "anything"})
    assert r.status_code == 501, r.text
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "OAUTH_NOT_CONFIGURED"
    assert "尚未配置" in body["error"]["message"]


def test_oauth_unsupported_provider_returns_404(client: TestClient) -> None:
    """白名单之外的渠道 → 404，且错误码与「未配置」可区分。"""
    r = client.post(OAUTH.format(provider="weibo"), json={"code": "anything"})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "OAUTH_PROVIDER_UNSUPPORTED"


def test_oauth_with_credentials_still_reports_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """凭据配齐了也还是 501 —— 只是原因不同（换码流程未实现，属 P5）。

    这条区分很重要：它让「配好凭据就能用」这个误会无法成立。
    """
    monkeypatch.setenv("CAREERLY_OAUTH_WECHAT_APP_ID", "wx_test_appid")
    monkeypatch.setenv("CAREERLY_OAUTH_WECHAT_APP_SECRET", "wx_test_secret")

    r = client.post(OAUTH.format(provider="wechat"), json={"code": "anything"})
    assert r.status_code == 501, r.text
    assert r.json()["error"]["code"] == "OAUTH_UNAVAILABLE"


def test_oauth_requires_a_code(client: TestClient) -> None:
    """空 body / 空 code → 422（在到达渠道判断之前就被 pydantic 拦下）。"""
    r = client.post(OAUTH.format(provider="wechat"), json={})
    assert r.status_code == 422, r.text
