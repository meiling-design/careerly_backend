"""Authentication endpoints: register / login / refresh / me / oauth.

Identity model
--------------
```text
register   name(必填) · phone(必填) · password(选填*) · email(选填)
login      phone(必填) · password(选填*)
```

The account identity is the **phone number**. Since 2026-10-04 a **password**
is the account's credential when one is set.

```text
有 password_hash 的账号   登录必须带对密码，否则 401
无 password_hash 的账号   遗留 / DEV 账号 —— 沿用 D85 的「手机号直登」，
                          且仍受 app.core.auth_mode.is_dev_auth() 门禁
                          （AUTH_MODE != dev → 501）
```

**注**：``password`` 在**请求模型**上是选填的，这是刻意的：后端保留
「无密码账号」这条路径，因此 6 个既有后端测试文件里
「只带 name + phone 注册」的调用不会被打破。**产品侧 UI 一律必填**
（注册页 / 登录页都把密码设为必填），所以真实用户拿不到无密码账号 ——
这一点见 ``docs/05_progress`` 的对应裁决记录。

Bearer tokens are issued as stateless JWTs (see ``app.core.security``).

All responses use the same envelope as ``router_v1``:

    {"success": bool, "data": ..., "error": {code, message} | None,
     "meta": {timestamp, version: "v1"}}
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr, Field

from app.core.auth_store import AuthStore
from app.core.passwords import MIN_PASSWORD_LENGTH, hash_password, verify_password
from app.core.security import get_current_user, issue_tokens

router = APIRouter(prefix="/api/v1")

_API_VERSION = "v1"

#: 登录 / 注册请求体里密码的长度上限。
#: 只是防滥用（避免超长 body），与产品规则无关 ——
#: bcrypt 本身只看前 72 字节，见 ``app.core.passwords``。
_MAX_PASSWORD_LENGTH = 128

#: 支持第三方登录的渠道 → 中文展示名。
#:
#: ⚠️ 这是**接缝**而不是能力：见 :func:`oauth_sign_in`。
_OAUTH_PROVIDERS: dict[str, str] = {"wechat": "微信", "qq": "QQ"}

# ── Envelope helpers (local copy, mirrors router_v1) ───────────────────────


def _ok(data: Any = None, status_code: int = 200, meta_extra: dict | None = None) -> JSONResponse:
    """Return a success-envelope JSON response."""
    meta: dict[str, Any] = {"timestamp": time.time(), "version": _API_VERSION}
    if meta_extra:
        meta.update(meta_extra)
    return JSONResponse(
        status_code=status_code,
        content={
            "success": True,
            "data": data,
            "error": None,
            "meta": meta,
        },
    )


def _err(code: str, message: str, status_code: int = 400) -> JSONResponse:
    """Return an error-envelope JSON response (not an exception)."""
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message},
            "meta": {"timestamp": time.time(), "version": _API_VERSION},
        },
    )


# ── Request models ─────────────────────────────────────────────────────────


class RegisterRequest(BaseModel):
    """POST /auth/register body.

    ``password`` 选填 —— 显式给了就必须满足最小长度（否则 pydantic 422），
    完全不给则创建**无密码账号**（见模块文档的说明）。
    """

    phone: str = Field(..., min_length=5, max_length=20, description="手机号，账号身份")
    name: str = Field(..., min_length=1, max_length=50, description="姓名 / 昵称")
    password: Optional[str] = Field(
        None,
        min_length=MIN_PASSWORD_LENGTH,
        max_length=_MAX_PASSWORD_LENGTH,
        description="密码（选填；给了就必须 >= 6 位）",
    )
    email: Optional[EmailStr] = Field(None, description="邮箱（选填）")


class LoginRequest(BaseModel):
    """POST /auth/login body.

    ⚠️ ``password`` **不设最小长度**：短密码属于「密码不对」，
    应当得到 401 而不是 422 —— 422 会把「密码错误」暴露成「格式不对」，
    对撞库者反而更有信息量。
    """

    phone: str = Field(..., min_length=5, max_length=20)
    password: Optional[str] = Field(
        None, max_length=_MAX_PASSWORD_LENGTH, description="密码（有密码的账号必填）"
    )


class OAuthRequest(BaseModel):
    """POST /auth/oauth/{provider} body."""

    code: str = Field(..., min_length=1, description="第三方回调返回的授权码")


class RefreshRequest(BaseModel):
    """POST /auth/refresh body."""

    refresh_token: str


# ── Helpers ────────────────────────────────────────────────────────────────


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    """Return the safe subset of a user dict for responses."""
    return {
        "id": user["id"],
        "phone": user["phone"],
        "name": user["name"],
        "email": user["email"],
    }


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.post("/auth/register", status_code=201)
async def register(req: RegisterRequest) -> JSONResponse:
    """Create a new account (identity = phone) and return a fresh token pair."""
    store = AuthStore()
    phone = req.phone.strip()

    if store.phone_exists(phone):
        return _err("PHONE_EXISTS", "该手机号已注册", 409)

    if req.email and store.email_exists(req.email):
        return _err("EMAIL_EXISTS", "该邮箱已被使用", 409)

    # 明文在离开本函数前就被哈希掉，不落库、不进日志。
    password_hash = hash_password(req.password) if req.password else None

    user_id = store.create_user(
        phone, req.name.strip(), req.email, password_hash=password_hash
    )
    user = store.get_by_id(user_id)
    if user is None:
        return _err("INTERNAL_ERROR", "Failed to create user", 500)

    tokens = issue_tokens(user)
    return _ok({"user": _public_user(user), **tokens}, status_code=201)


@router.post("/auth/login")
async def login(req: LoginRequest) -> JSONResponse:
    """Authenticate by phone number (+ password when the account has one).

    凭据形态由**账号自己**决定（见模块文档），而不是由全局配置决定：

    ```text
    有 password_hash  →  密码是唯一凭据；错 / 缺 → 401。
                        这是**真实凭据校验**，不受 AUTH_MODE 门禁。
    无 password_hash  →  遗留 / DEV 账号，沿用 D85 的手机号直登，
                        仍受 is_dev_auth() 门禁（AUTH_MODE != dev → 501）
    ```

    ⚠️ 未决项 C1b（保持原判）：PRD / IA 均未定义「手机号未注册」的产品规则，
    此处仍是 401 PHONE_NOT_REGISTERED，等待产品侧裁决。
    它与 INVALID_CREDENTIALS 是两个不同的错误码 —— 也就是说，
    本端点**会暴露「某个手机号是否已注册」**。这是既有行为，
    不为本次改动新开的口子，故原样保留并在此标注。

    ⚠️ **分支顺序是刻意的，改动前先读完这段**：两个分支的先后
    决定了「非 DEV 形态下会不会泄露手机号是否已注册」。
    ``stored_hash is None``（无密码账号 / 账号根本不存在）这一支
    **必须先做 AUTH_MODE 门禁、再区分「未注册」与「已存在」** ——
    这正是 P0-6 想保住的性质：``AUTH_MODE=formal`` 的生产部署下，
    探测任意手机号都得到同一个 501，而不是 401/403 的差异。
    把 ``if user is None`` 提到前面（一个很自然的「先判空」写法）
    就会悄悄把这条性质破坏掉，而 PRD 里没有任何地方会提醒你。
    """
    from app.core.auth_mode import is_dev_auth

    store = AuthStore()
    user = store.get_by_phone(req.phone.strip())
    # 只在这一个地方取哈希；``user`` 为 None 时不必查库。
    stored_hash = store.get_password_hash(user["id"]) if user else None

    if stored_hash is None:
        # ── 无密码账号 / 账号不存在 —— 同属「DEV 直登」这条遗留路径 ──
        if not is_dev_auth():
            # 合并处理，且**先于**存在性判断：见上文关于分支顺序的说明。
            return _err(
                "AUTH_MODE_UNAVAILABLE",
                "当前部署未启用开发直登；正式身份验证通道尚未上线",
                501,
            )
        if user is None:
            return _err("PHONE_NOT_REGISTERED", "该手机号尚未注册", 401)
        if not user.get("is_active", 1):
            return _err("ACCOUNT_DISABLED", "账号已停用", 403)
    else:
        # ── 有密码的账号 —— 真实凭据校验，不受 AUTH_MODE 门禁 ──
        if not req.password or not verify_password(req.password, stored_hash):
            return _err("INVALID_CREDENTIALS", "手机号或密码不正确", 401)
        # 停用状态放在密码校验**之后**：没带对密码的人不该能问出
        # 「这个手机号是否已注册且被停用」。
        if not user.get("is_active", 1):
            return _err("ACCOUNT_DISABLED", "账号已停用", 403)

    tokens = issue_tokens(user)
    return _ok({"user": _public_user(user), **tokens})


# ── Third-party sign-in（可配置契约） ────────────────────────────────────────


def _oauth_credentials(provider: str) -> tuple[str | None, str | None]:
    """读取某渠道的 AppID / AppSecret（环境变量）。

    ```text
    wechat  →  CAREERLY_OAUTH_WECHAT_APP_ID / CAREERLY_OAUTH_WECHAT_APP_SECRET
    qq      →  CAREERLY_OAUTH_QQ_APP_ID     / CAREERLY_OAUTH_QQ_APP_SECRET
    ```
    """
    prefix = f"CAREERLY_OAUTH_{provider.upper()}"
    return os.getenv(f"{prefix}_APP_ID"), os.getenv(f"{prefix}_APP_SECRET")


@router.post("/auth/oauth/{provider}")
async def oauth_sign_in(provider: str, req: OAuthRequest) -> JSONResponse:
    """第三方登录 —— **目前是接缝，不是能力**。

    登录页与注册页都画了「微信 / QQ」按钮，但项目当前**没有**任何第三方
    开放平台凭据，也没有对应的 SDK 接入。产品侧 2026-10-04 裁决：
    **先把契约建出来，未配置时明确报错**，而不是让按钮静默失败。

    因此本端点在两种情况下都返回 501，只是原因不同：

    ```text
    环境变量没配   OAUTH_NOT_CONFIGURED  —— 换渠道 / 配置凭据即可推进
    配置齐了但…    OAUTH_UNAVAILABLE     —— 换码流程尚未实现（P5 范围）
    ```

    ⚠️ 与 ``docs/Careerly 全栈开发总控 Prompt.md`` 的关系：该文件写明
    「不在当前阶段实现 OAuth / 社交登录」。本端点**不违背**它 ——
    它没有实现登录，只是把「未实现」这件事变成了一个**可被客户端识别的错误**。
    正式接入（授权码换取 token、账号绑定）属 Roadmap P5。
    """
    display = _OAUTH_PROVIDERS.get(provider)
    if display is None:
        return _err(
            "OAUTH_PROVIDER_UNSUPPORTED",
            f"不支持的第三方登录渠道：{provider}",
            404,
        )

    app_id, app_secret = _oauth_credentials(provider)
    if not app_id or not app_secret:
        return _err(
            "OAUTH_NOT_CONFIGURED",
            f"{display}登录尚未配置，请使用手机号注册或登录",
            501,
        )

    return _err(
        "OAUTH_UNAVAILABLE",
        f"{display}登录尚未开通，请使用手机号注册或登录",
        501,
    )


@router.post("/auth/refresh")
async def refresh(req: RefreshRequest) -> JSONResponse:
    """Exchange a valid refresh token for a fresh access + refresh pair."""
    from app.core.security import decode_token

    store = AuthStore()

    try:
        payload = decode_token(req.refresh_token)
    except Exception:
        return _err("INVALID_REFRESH_TOKEN", "Refresh token is invalid or expired", 401)

    if payload.get("type") != "refresh":
        return _err("INVALID_REFRESH_TOKEN", "Provided token is not a refresh token", 401)

    subject = payload.get("sub")
    if not subject or not str(subject).isdigit():
        return _err("INVALID_REFRESH_TOKEN", "Invalid refresh token subject", 401)

    user = store.get_by_id(int(subject))
    if user is None or not user.get("is_active", 1):
        return _err("INVALID_REFRESH_TOKEN", "User no longer exists or is disabled", 401)

    tokens = issue_tokens(user)
    return _ok({
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "token_type": "bearer",
        "expires_in": tokens["expires_in"],
    })


@router.get("/auth/me")
async def me(current_user: dict = Depends(get_current_user)) -> JSONResponse:
    """Return the currently authenticated user (resolved from Bearer token)."""
    return _ok({"user": _public_user(current_user)})
