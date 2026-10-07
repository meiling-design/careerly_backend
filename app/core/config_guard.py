"""启动期安全配置硬阻断 —— 异常类型定义（发布基线 R0-2）。

本模块**只有**异常类型，没有逻辑。

为什么需要一个专门的模块
------------------------
这三条防护全部在**模块导入期**触发（进程起不来，而不是第一个请求才报错）：

```text
InsecureAuthConfiguration    ENV=production + AUTH_MODE=dev
InsecureSecretConfiguration  ENV=production + JWT 密钥缺失/默认/过短
InsecureCorsConfiguration    ENV=production + CORS 白名单未配置
```

抛出点分散在 ``app.core.auth_mode`` / ``app.core.security`` / ``app.api.server``,
而**异常类型必须在这里**，原因是它们的身份要跨「模块重载」保持稳定：

* 测试里验证 fail-fast 的唯一办法是 ``importlib.reload(<抛出点模块>)``
  （配置是在导入期读的，不重载就测不到）；
* 生产上 ``uvicorn --reload`` / 各类热重载同样会重新执行这些模块。

若异常类定义在被重载的那个模块里，每次重载都会产生一个**新的类对象**，
于是 ``except <在重载前捕获的那个类>`` 静默失配 —— 调用方拿到的是一个
「看起来同名、实则不同」的异常。这正是本次把它抽出来的直接原因：
``tests/test_release_security_baseline.py`` 一开始就是这样静默失败的。

放在这个谁都不重载的模块里，三个类型因此各只有**一个**身份。

与 ``InsecureAuthConfiguration`` 的关系
---------------------------------------
它原本定义在 ``app.core.auth_mode``（正因为 auth_mode 从不被重载，
``tests/test_p0_auth_boundary.py`` 才恰好能通过）。本次一并搬到这里，
``auth_mode`` 保留同名再导出，既有导入路径不受影响。
"""

from __future__ import annotations

__all__ = [
    "InsecureAuthConfiguration",
    "InsecureCorsConfiguration",
    "InsecureSecretConfiguration",
]


class InsecureAuthConfiguration(RuntimeError):
    """生产环境启用了 DEV 认证（``AUTH_MODE=dev``）—— 拒绝启动。"""


class InsecureSecretConfiguration(RuntimeError):
    """生产环境使用了缺失 / 默认 / 过短的 JWT 签名密钥 —— 拒绝启动。"""


class InsecureCorsConfiguration(RuntimeError):
    """生产环境未显式配置 CORS 来源白名单 —— 拒绝启动。"""
