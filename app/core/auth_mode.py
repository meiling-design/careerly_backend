"""DEV / PROD 认证边界（决策 D-NEW 方案 A · P0-6）。

────────────────────────────────────────────────────────────────────────────
背景
────────────────────────────────────────────────────────────────────────────
**无密码账号**的 ``POST /auth/login`` 只凭手机号即签发完整 token 对。
这在**开发联调**阶段是刻意选择，但**绝不能**成为生产形态 ——
那等于「知道手机号即可接管账号」。

> ⚠️ 2026-10-04 起，「手机号直登」**只对无密码账号成立**。
> 新注册流程会写入 ``password_hash``，这类账号的登录走真实密码校验
> （``app/core/passwords.py``），**不受本模块的 DEV 门禁约束** ——
> 它是真凭据，不该被"DEV 模式"这个开关连坐。
> 本模块因此只守护**无密码账号**这一条遗留路径。

产品侧裁决（D-NEW 方案 A）：
    🟢 开发环境允许手机号直登，但必须显式标记为 **DEV ONLY**；
    🔴 生产环境**禁止**无验证登录。正式认证通道留到 P5。

────────────────────────────────────────────────────────────────────────────
本模块的职责
────────────────────────────────────────────────────────────────────────────
把「DEV 认证」从**隐含默认**变成**显式配置**，并提供一个**硬阻断**：

    ENV=production + AUTH_MODE=dev   →  进程**拒绝启动**（不是 warning）

为什么必须是 fail-fast 而不是告警：告警会被忽略，`先这样跑着，等上线再补`
正是这种心态的产物。启动即失败是唯一能真正拦住它的机制。

配置项（均为环境变量）：
    ENV        运行环境。``production`` / ``prod`` 视为生产；默认 development。
    AUTH_MODE  认证模式。``dev``（默认）或 ``formal``。
               注意默认值是 dev —— 这样本地开发零配置，而生产必须**显式**
               设置 ENV=production，从而被下面的检查拦住。
"""

from __future__ import annotations

import os

AUTH_MODE_DEV = "dev"
AUTH_MODE_FORMAL = "formal"

_PRODUCTION_ENVS = frozenset({"production", "prod"})


class InsecureAuthConfiguration(RuntimeError):
    """生产环境启用了 DEV 认证 —— 拒绝启动。"""


def get_env() -> str:
    """运行环境名（小写）。``ENV`` 优先，其次 ``APP_ENV``，默认 development。"""
    raw = os.getenv("ENV") or os.getenv("APP_ENV") or "development"
    return raw.strip().lower()


def get_auth_mode() -> str:
    """认证模式（小写）。默认 ``dev``。"""
    return (os.getenv("AUTH_MODE") or AUTH_MODE_DEV).strip().lower()


def is_production() -> bool:
    """当前是否生产环境。"""
    return get_env() in _PRODUCTION_ENVS


def is_dev_auth() -> bool:
    """当前是否为 DEV 认证（**无密码账号**可凭手机号直登）。

    ⚠️ 只作用于无密码账号。有 ``password_hash`` 的账号走密码校验，
    与本函数的取值无关（见模块文档 2026-10-04 的说明）。
    """
    return get_auth_mode() != AUTH_MODE_FORMAL


def assert_auth_configuration_is_safe() -> None:
    """启动自检：生产环境不得启用 DEV 认证。

    由 ``app/api/server.py`` 在**模块导入期**调用，因此配置违规时进程起不来，
    而不是等到第一个用户登录才暴露。

    Raises:
        InsecureAuthConfiguration: ``ENV=production`` 且 ``AUTH_MODE=dev``。
    """
    if is_production() and is_dev_auth():
        raise InsecureAuthConfiguration(
            "拒绝启动：检测到 ENV=production 且 AUTH_MODE=dev。\n"
            "  该组合下，**无密码账号**可凭手机号直登（无凭证校验），"
            "仅可用于开发联调。\n"
            "  生产环境必须：\n"
            "    1) 确保没有无密码账号（注册流程已写 password_hash）；\n"
            "    2) 设置 AUTH_MODE=formal。\n"
            "  在正式通道（Roadmap P5-4）就绪前，请勿以 ENV=production 部署。"
        )


def auth_mode_banner() -> str:
    """给启动日志用的一行说明，让当前认证形态在日志里**显式可见**。"""
    if is_dev_auth():
        return (
            f"AUTH MODE = DEV ({AUTH_MODE_DEV}) | ENV = {get_env()} | "
            "手机号直登、无凭证校验 —— 仅供开发联调，禁止用于生产"
        )
    return f"AUTH MODE = FORMAL | ENV = {get_env()}"


__all__ = [
    "AUTH_MODE_DEV",
    "AUTH_MODE_FORMAL",
    "InsecureAuthConfiguration",
    "get_env",
    "get_auth_mode",
    "is_production",
    "is_dev_auth",
    "assert_auth_configuration_is_safe",
    "auth_mode_banner",
]
