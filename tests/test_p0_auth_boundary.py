"""P0-6 —— DEV / PROD 认证边界的回归测试（决策 D-NEW 方案 A）。

被保护的不变量：
    ENV=production + AUTH_MODE=dev  →  进程**拒绝启动**

为什么值得写测试：当前认证是「手机号直登、无凭证校验」（裁决 D85），
它在开发期是正确的，在生产期是致命的。唯一能真正拦住
「先这样跑着，等产品快上线再补」的机制就是**启动即失败** ——
所以这条断言必须被测试钉住，否则某次重构很容易把它降级成 warning。

运行：
    ./venv/Scripts/python.exe -m pytest tests/test_p0_auth_boundary.py -v
"""

from __future__ import annotations

import importlib

import pytest

from app.core import auth_mode
from app.core.auth_mode import InsecureAuthConfiguration


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ENV", "APP_ENV", "AUTH_MODE"):
        monkeypatch.delenv(key, raising=False)


# ── 配置解析 ────────────────────────────────────────────────────────────────


def test_defaults_are_development_and_dev_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """零配置默认 = 本地开发可用（否则新同学 clone 下来跑不起来）。"""
    _clear_env(monkeypatch)
    assert auth_mode.get_env() == "development"
    assert auth_mode.is_production() is False
    assert auth_mode.is_dev_auth() is True


@pytest.mark.parametrize("env_name", ["production", "prod", "Production", " PROD "])
def test_production_env_aliases_are_recognised(monkeypatch: pytest.MonkeyPatch, env_name: str) -> None:
    """大小写与空白不该成为绕过边界检查的方式。"""
    monkeypatch.setenv("ENV", env_name)
    assert auth_mode.is_production() is True


# ── 硬阻断 ──────────────────────────────────────────────────────────────────


def test_production_with_dev_auth_refuses_to_start(monkeypatch: pytest.MonkeyPatch) -> None:
    """核心断言：生产 + DEV 认证 → 抛错（不是打印告警）。"""
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("AUTH_MODE", raising=False)  # 默认 dev
    with pytest.raises(InsecureAuthConfiguration):
        auth_mode.assert_auth_configuration_is_safe()


def test_production_with_formal_auth_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """接入正式认证后，生产环境可正常启动。"""
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("AUTH_MODE", "formal")
    auth_mode.assert_auth_configuration_is_safe()  # 不抛即通过


def test_development_never_blocks_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """开发环境无论 AUTH_MODE 为何都放行 —— 本检查只针对生产。"""
    monkeypatch.setenv("ENV", "development")
    for mode in ("dev", "formal"):
        monkeypatch.setenv("AUTH_MODE", mode)
        auth_mode.assert_auth_configuration_is_safe()


def test_server_module_refuses_to_import_under_insecure_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端形态：检查挂在**模块导入期**，因此进程根本起不来。

    这是 P0-6 的真正交付物 —— 只测 ``assert_auth_configuration_is_safe``
    不足以保证它真的被调用；这条测试会因「有人删掉了 server.py 里的调用」
    而失败。
    """
    monkeypatch.setenv("ENV", "production")
    monkeypatch.delenv("AUTH_MODE", raising=False)

    import app.api.server as server_module

    with pytest.raises(InsecureAuthConfiguration):
        importlib.reload(server_module)


# ── 可观测性 ────────────────────────────────────────────────────────────────


def test_banner_makes_dev_auth_visible(monkeypatch: pytest.MonkeyPatch) -> None:
    """当前认证形态必须在启动日志里显式可见，而不是隐含在默认值里。"""
    monkeypatch.setenv("ENV", "development")
    monkeypatch.setenv("AUTH_MODE", "dev")
    banner = auth_mode.auth_mode_banner()
    assert "DEV" in banner
    assert "禁止用于生产" in banner

    monkeypatch.setenv("AUTH_MODE", "formal")
    assert "FORMAL" in auth_mode.auth_mode_banner()
