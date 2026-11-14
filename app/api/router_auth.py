"""Authentication endpoints: register / login / refresh / me / oauth.

Identity model
--------------
```text
register       name(必填) · phone(必填) · password(选填*) · email(选填)
login          phone(必填) · password(选填*)
reset-password phone(必填) · new_password(必填, >= 6)   ← 仅 DEV 模式
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

from app.core import account_purge
from app.core.auth_store import AuthStore
from app.core.passwords import MIN_PASSWORD_LENGTH, hash_password, verify_password
from app.core.security import get_current_user, issue_tokens
from app.utils.logger import logger
from app.utils.redact import mask_phone

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


class ResetPasswordRequest(BaseModel):
    """POST /auth/reset-password body（仅 DEV 模式可用）。

    ⚠️ 这是**开发联调**通道，不是正式找回流程：DEV 模式下手机号本身
    就是可直登的凭据（见 ``app.core.auth_mode``），因此允许凭手机号重置密码
    并不额外降低安全性。生产形态（``AUTH_MODE=formal``）一律 501 ——
    正式找回需要短信 / 邮箱验证码，属独立的 P5 通道。
    """

    phone: str = Field(..., min_length=5, max_length=20, description="手机号，账号身份")
    new_password: str = Field(
        ...,
        min_length=MIN_PASSWORD_LENGTH,
        max_length=_MAX_PASSWORD_LENGTH,
        description="新密码（>= 6 位）",
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

    # ── 已注销账号（决策 D119）───────────────────────────────────────────
    # 放在两个分支**汇合之后**，位置是刻意的：
    #
    # * 必须在这里 —— 软删除不动 ``password_hash``，所以注销后密码照样能
    #   校验通过。少了这一条，用户在 App 里「删除账号」之后重新登录就能拿到
    #   一个带**新版本号**的 token，token_version 吊销被整个绕过。
    # * 必须在密码校验**之后** —— 与上面 is_active 同理：没带对密码的人
    #   不该能问出「这个号是否已注册且已注销」。
    #
    # 401 而非 403：账号已不存在，客户端应当清掉本地凭据回到登录页，
    # 这与 get_current_user 对已注销账号返回 401 保持一致。
    if user.get("status") == "deleted":
        return _err("ACCOUNT_DELETED", "账号已注销", 401)

    tokens = issue_tokens(user)
    return _ok({"user": _public_user(user), **tokens})


@router.post("/auth/reset-password")
async def reset_password(req: ResetPasswordRequest) -> JSONResponse:
    """DEV-only password reset: phone + new password, no OTP.

    背景：App 的「忘记密码?」此前是死链（Figma 没给 onClick，后端也没有
    端点）。2026-11-13 产品侧要求补齐，并选定「DEV 模式手机号直重置」——
    当前没有短信 / 邮件服务，密码是 bcrypt 哈希无法找回，只能重置。

    ```text
    AUTH_MODE=dev     凭手机号即可重置（与 DEV 直登同一条信任边界）
    AUTH_MODE=formal  501 PASSWORD_RESET_UNAVAILABLE（不泄露手机号是否存在）
    ```

    ⚠️ **不是正式找回通道**。生产变体需要短信 / 邮箱验证码，属 P5。
    把本端点当成生产可用的找回手段是错的 —— ``auth_mode`` 的 fail-fast
    与这里的门禁共同保证它不会在 ``ENV=production`` 下被走到。

    重置成功会通过 :meth:`AuthStore.set_password_hash` **同时吊销全部旧令牌**，
    因此旧设备上的会话立即失效。
    """
    from app.core.auth_mode import is_dev_auth

    # 先做模式门禁、再查账号 —— 与 login 的分支顺序同理：
    # 正式形态下探测任意手机号都应得到同一个 501。
    if not is_dev_auth():
        return _err(
            "PASSWORD_RESET_UNAVAILABLE",
            "密码找回当前仅在开发模式可用；正式找回通道尚未上线",
            501,
        )

    store = AuthStore()
    phone = req.phone.strip()
    user = store.get_by_phone(phone)
    if user is None:
        return _err("PHONE_NOT_REGISTERED", "该手机号尚未注册", 401)
    if not user.get("is_active", 1):
        return _err("ACCOUNT_DISABLED", "账号已停用", 403)
    if user.get("status") == "deleted":
        return _err("ACCOUNT_DELETED", "账号已注销", 401)

    # 明文在离开本函数前被哈希；日志只记录脱敏手机号。
    store.set_password_hash(user["id"], hash_password(req.new_password))
    logger.info(
        "AuthStore: password reset | id=%d | phone=%s",
        user["id"],
        mask_phone(phone),
    )
    return _ok({"reset": True})


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
    # 已注销账号（决策 D119）。下面的版本比对**已经**能拦住它（软删除必然
    # +1），这里再判一次 status 是冗余的第二道闸：万一将来出现「注销后又
    # 恢复账号」这类把版本号对齐的路径，凭据也不该复活。
    if user.get("status") == "deleted":
        return _err("INVALID_REFRESH_TOKEN", "User no longer exists or is disabled", 401)

    # 吊销比对（决策 D119）—— refresh 必须与 access 同口径。
    # 少了这一段，用户「删除账号」后旧 refresh 仍能无限换取新 access token，
    # 吊销就等于没做：access 只有 60 分钟，而 refresh 有 90 天。
    if int(payload.get("token_version", 0)) != int(user.get("token_version", 0) or 0):
        return _err("INVALID_REFRESH_TOKEN", "Refresh token has been revoked", 401)

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


# ── 注销账号（决策 D119 / 发布基线 K19）─────────────────────────────────────

#: 软删除到物理清理之间的窗口（天）。用户视角「马上生效」，数据在窗口内可恢复。
PURGE_GRACE_DAYS = 30


@router.delete("/account/career-data")
def delete_career_data(
    current_user: dict = Depends(get_current_user),
) -> JSONResponse:
    """删除当前账号的职业数据，但保留账号与当前会话。"""
    user_id = int(current_user["id"])
    report = account_purge.purge_user(user_id, dry_run=False)

    warnings: list[str] = []
    if report.orphan_artifacts:
        warnings.append("部分历史文件无法确认归属，未被删除。")
    if any("文件删除失败" in warning for warning in report.warnings):
        warnings.append("部分职业数据文件删除失败，请联系支持。")

    logger.info(
        "Career data deleted | id=%d | rows=%d | files=%d | warnings=%d",
        user_id,
        report.total_rows,
        report.total_files,
        len(warnings),
    )
    return _ok(
        {
            "deleted": True,
            "account_retained": True,
            "deleted_rows": report.total_rows,
            "deleted_files": report.total_files,
            "orphan_artifacts": report.orphan_artifacts,
            "warnings": warnings,
        }
    )


@router.delete("/account")
async def delete_account(current_user: dict = Depends(get_current_user)) -> JSONResponse:
    """注销当前账号 —— **软删除 + 立即吊销全部凭据**。

    为什么不做物理删除
    ------------------
    用户数据散在四个 SQLite 库 + 三类落盘文件里，没有外键把它们连起来
    （见 ``app/core/account_purge.py`` 的清单）。当场做级联删除有两个问题：
    中途失败会留下**比不删更难处理**的半删状态；而用户已经看到「删除成功」了。

    所以拆成两步：

    ```text
    现在       status='deleted' + deleted_at + token_version += 1
               → 用户视角立刻生效（旧 token 下一次请求就失效）
    30 天后    scripts/purge_deleted_accounts.py 离线做物理级联
               → 那时失败可以重跑，且窗口期内误删可恢复
    ```

    ⚠️ ``token_version += 1`` 与 ``status='deleted'`` 必须在**同一次提交**里
    （见 ``AuthStore.soft_delete_user``）：只标记不吊销，用户在 90 天 refresh
    有效期内还能继续访问；只吊销不标记，30 天后的清理任务找不到这个账号。

    与 Google Play 的关系：政策要求 App 内提供账号删除入口，且删除请求
    必须同时覆盖账号与关联数据 —— 后者由 30 天清理任务承担，其范围见
    ``account_purge`` 的实测报告。
    """
    store = AuthStore()
    user_id = int(current_user["id"])

    if not store.soft_delete_user(user_id):
        # 已经是 deleted 状态。这里返回 409 而不是假装成功：重复调用不该
        # 被当成「又一次成功删除」，否则客户端无从判断自己是不是第一个删的。
        return _err("ACCOUNT_ALREADY_DELETED", "账号已注销", 409)

    # 手机号必须脱敏（K21）—— 注销日志同样不该留明文联系方式。
    logger.info(
        "Account deleted（软删除）| id=%d | phone=%s | 物理清理将在 %d 天后",
        user_id,
        mask_phone(current_user.get("phone")),
        PURGE_GRACE_DAYS,
    )

    return _ok(
        {
            "deleted": True,
            "purge_after_days": PURGE_GRACE_DAYS,
            "purge_after_ts": time.time() + PURGE_GRACE_DAYS * 86400,
        }
    )
