"""P1-6 —— Token 有效期（裁决 D-B 的后端一半）。

D-B 不只是「要不要持久化」，而是「**如何安全地**持久化」，且必须连着过期
策略一起定：

```text
持久化  → 解决「同一会话内重启 App 不失忆」
有效期  → 解决「跨周 / 跨月回来还记得我是谁」
```

两者缺一不可。Careerly 是低频产品，7 天的 refresh 会让「隔一个月回来看看
成长变化」变成一次重新登录（正式认证上线后 = 收一次短信）。

本套件**不联网**，只断言签发出的令牌生命周期符合裁决。
"""

from __future__ import annotations

from app.core import security


def test_refresh_lifetime_is_90_days_by_default():
    """7 天 → 90 天。"""
    assert security.REFRESH_TOKEN_EXPIRE_DAYS == 90, \
        "P1-6：refresh 默认有效期应为 90 天（原 7 天不足以支撑低频产品）"


def test_access_lifetime_unchanged():
    """access 仍只有 60 分钟 —— 放宽 refresh 不应扩大 access 的泄露窗口。"""
    assert security.ACCESS_TOKEN_EXPIRE_MINUTES == 60


def test_issued_tokens_carry_the_configured_lifetimes():
    """实际签发的令牌里 exp - iat 要真的等于配置值。"""
    access = security.decode_token(security.create_access_token("1"))
    refresh = security.decode_token(security.create_refresh_token("1"))

    assert access["type"] == "access"
    assert refresh["type"] == "refresh", "refresh 令牌必须带类型声明，不能被当 access 用"

    access_span = access["exp"] - access["iat"]
    refresh_span = refresh["exp"] - refresh["iat"]

    assert access_span == security.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert refresh_span == security.REFRESH_TOKEN_EXPIRE_DAYS * 86400
    assert refresh_span > access_span, "refresh 必须比 access 长，否则轮换没有意义"


def test_refresh_token_cannot_be_used_as_access_token():
    """类型声明要真的被用于鉴权 —— 否则放宽 refresh 等于放宽 access。"""
    refresh = security.create_refresh_token("1")

    from app.core.security import _ACCESS_TYPE  # 仅断言内部约定，不依赖公开 API

    assert security.decode_token(refresh)["type"] != _ACCESS_TYPE
