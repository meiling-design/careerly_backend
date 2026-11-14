"""FastAPI REST server for the Job Matching AI Agent.

Provides a production-grade HTTP API wrapping the LangGraph pipeline.

启动:
    uvicorn app.api.server:app --reload --port 8000
"""

from __future__ import annotations

import os
from typing import Any, Optional

import time

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.api.middleware import add_api_middleware
from app.core.auth_mode import (
    assert_auth_configuration_is_safe,
    auth_mode_banner,
    is_production,
)
from app.core.config_guard import InsecureCorsConfiguration
from app.core.errors import AppError, ResourceNotFound
from app.core.orchestrator import DecisionGraph
from app.core.security import get_current_user
from app.core.state_store import StateStore
from app.utils.logger import logger
from app.utils.paths import APP_DATA_DIR, DATA_ROOT, ROOT_DATA_DIR, TESSDATA_DIR

# ── 认证边界自检（决策 D-NEW 方案 A / P0-6）──────────────────────────────────
# 必须在创建 app 之前执行：ENV=production + AUTH_MODE=dev → **拒绝启动**。
# 这里刻意用 fail-fast 而不是告警 —— 「先这样跑着，等产品快上线再补」正是
# 告警拦不住、而这行 raise 能拦住的那类决定。
assert_auth_configuration_is_safe()
logger.info("Auth boundary | %s", auth_mode_banner())

# ── 数据根自检 ────────────────────────────────────────────────────────────────
# 未设 DATA_DIR/RAILWAY_VOLUME_MOUNT_PATH 时 DATA_ROOT 为 None，此时两棵树在
# 仓库内（ECS 上的正常形态，见 docs/09_Release/DEPLOY_ECS.md §3）。
# 打这一条是为了让 §5 那种 `journalctl | grep` 式自检也能确认数据落点 ——
# 「库写到了别处」是部署后最难发现的一类问题。
if DATA_ROOT is None:
    logger.info(
        "Data root | (未配置) app=%s | root=%s | tessdata=%s",
        APP_DATA_DIR, ROOT_DATA_DIR, TESSDATA_DIR,
    )
else:
    logger.info(
        "Data root | %s (来自 DATA_DIR/RAILWAY_VOLUME_MOUNT_PATH) "
        "| app=%s | root=%s | tessdata=%s (固定锚定仓库)",
        DATA_ROOT, APP_DATA_DIR, ROOT_DATA_DIR, TESSDATA_DIR,
    )

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Job Matching AI Agent API",
    description="Multi-agent pipeline: crawl jobs → parse resume → 2-stage matching → ranking report",
    version="1.0.0",
)

# CORS policy:
#   - If CORS_ALLOW_ORIGINS is set (comma-separated list), use it together
#     with allow_credentials=True.
#   - Otherwise fall back to a permissive "*" but with credentials DISABLED
#     (the "*" + credentials=True combo is invalid per the CORS spec and is
#     silently ignored/blocked by browsers).
#
# 生产环境**不允许**这个回退（发布基线 K24）。`*` 在开发时很方便，但生产
# 意味着任何站点都能从用户浏览器直接调用本 API。与 JWT 密钥同样处理 ——
# 缺失即拒绝启动，而不是静默降级成一个「能跑但不安全」的形态。
_cors_origins_raw = os.getenv("CORS_ALLOW_ORIGINS", "").strip()
if _cors_origins_raw:
    _allow_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
    _allow_credentials = True
elif is_production():
    raise InsecureCorsConfiguration(
        "拒绝启动：ENV=production 但未设置 CORS_ALLOW_ORIGINS。\n"
        "  留空会回退到 allow_origins=['*'] —— 任何站点都能从用户浏览器"
        "直接调用本 API。\n"
        "  生产必须显式列出允许来源（逗号分隔），例如：\n"
        "    CORS_ALLOW_ORIGINS=https://app.example.com\n"
        "  注：移动端 App 不走浏览器同源策略，不受本项约束；"
        "这里限制的是 Web / 管理端来源。"
    )
else:
    _allow_origins = ["*"]
    _allow_credentials = False

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allow_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Service-level middleware (opt-in: API_KEY / RATE_LIMIT env vars) ─────────
# Only activates when API_KEY and/or RATE_LIMIT are configured, so local dev
# is unaffected. This is independent of the user JWT auth below.
add_api_middleware(app)

# ── Mount versioned API v1 ──────────────────────────────────────────────────
from app.api.router_v1 import router as v1_router
app.include_router(v1_router)

# ── Mount App API ──────────────────────────────────────────────────────────
from app.api.router_app import router as app_router
app.include_router(app_router)

# ── Mount Auth API ─────────────────────────────────────────────────────────
from app.api.router_auth import router as auth_router
app.include_router(auth_router)

# ── Mount Careerly API (contract-aligned surface for the Flutter app) ──────
from app.careerly.router import router as careerly_router
app.include_router(careerly_router)

# ── Global exception handlers (unified error envelope) ──────────────────────


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "success": False,
            "data": None,
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                "details": exc.errors(),
            },
            "meta": {"timestamp": time.time(), "version": "v1"},
        },
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception | path=%s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "success": False,
            "data": None,
            "error": {"code": "INTERNAL_ERROR", "message": str(exc)[:500]},
            "meta": {"timestamp": time.time(), "version": "v1"},
        },
    )


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    """把领域异常统一映射为信封格式（决策 D-G）。

    ``PermissionDenied`` / ``ResourceNotFound`` 都携带 404 + ``NOT_FOUND`` ——
    「无权访问」与「不存在」对外**不可区分**，防止通过状态码枚举他人资源。
    这也是为什么这里不做 403 分支：那会重新引入信息泄露。
    """
    if exc.status_code >= 500:
        logger.error("AppError | path=%s | code=%s | %s", request.url.path, exc.code, exc.message)
    else:
        logger.info("AppError | path=%s | code=%s | status=%d", request.url.path, exc.code, exc.status_code)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "success": False,
            "data": None,
            "error": exc.to_error_dict(),
            "meta": {"timestamp": time.time(), "version": "v1"},
        },
    )


# ── Root redirect ──────────────────────────────────────────────────────────


@app.get("/", include_in_schema=False)
async def root_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/api/v1/health")


logger.info("API v1 mounted at /api/v1 — see /api/v1/health or /docs")

# Global singletons (lazy-initialised on first request)
_graph: DecisionGraph | None = None
_store: StateStore | None = None


def get_graph() -> DecisionGraph:
    global _graph
    if _graph is None:
        _graph = DecisionGraph()
    return _graph


def get_store() -> StateStore:
    global _store
    if _store is None:
        _store = StateStore()
    return _store


# ── Request / Response models ──────────────────────────────────────────────────


class MatchRequest(BaseModel):
    """Request body for POST /match."""

    job_urls: list[str] = Field(..., min_length=1, description="Job posting URLs to analyze")
    resume_file_path: Optional[str] = Field(None, description="Path to resume file on server")
    resume_text: Optional[str] = Field(None, description="Raw resume text (alternative)")
    session_id: Optional[str] = Field(None, description="Resume a prior session ID")
    use_llm: bool = Field(True, description="Enable LLM-based matching")
    use_embedding: bool = Field(True, description="Enable embedding-based matching")


class MatchResponse(BaseModel):
    """Response from POST /match."""

    session_id: str
    status: str
    routing_decision: str
    routing_reason: str
    jobs_found: int
    jobs_matched: int
    top_score: Optional[float] = None
    top_job: Optional[str] = None
    average_score: Optional[float] = None
    errors: list[str] = []


class SessionSummary(BaseModel):
    """Summary of one session for list display."""

    session_id: str
    status: str
    jobs_found: int = 0
    jobs_matched: int = 0
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    error_count: int = 0


# ── Endpoints ──────────────────────────────────────────────────────────────────


# ════════════════════════════════════════════════════════════════════════════
# 根路径端点 —— /api/v1/* 的历史别名（P1-3）
# ════════════════════════════════════════════════════════════════════════════
#
# ``/match`` 等根路径自项目早期就存在，README 与 docs.md 的示例仍在引用。
# 决策 **D-H** 明确「旧 API 不删除，默认修复」，所以这里保留路由，
# 但把它们**收敛为别名语义**：
#
#   1. OpenAPI 标记 ``deprecated=True`` —— 调用方在文档里就能看见
#   2. 错误一律走领域异常（``AppError``）→ 全局处理器转统一信封，
#      与 ``/api/v1/*`` **形状一致**（此前裸抛 HTTPException 返回的是
#      ``{"detail": ...}``，两套入口两套错误格式，客户端要写两套解析）
#
# ⚠️ **没有**合并实现：两侧的输入契约不同 —— ``/api/v1/match`` 的
# ``MatchRequest`` 强制 ``job_urls`` 非空（min_length=1）且过 X-Client-Key /
# demo 配额 / 输入长度三重守门；根 ``/match`` 允许只传简历、无这些门禁。
# 贸然让根路径委派过去会让老脚本直接 422。真正合并要等产品侧确认
# 「引擎调试入口」还要不要保留，故列 P4。


@app.get("/health", deprecated=True)
async def health() -> dict:
    """Health check endpoint (legacy alias of ``GET /api/v1/health``)."""
    return {
        "status": "ok",
        "service": "job-matching-ai-agent",
        "version": "1.0.0",
    }


@app.post("/match", response_model=MatchResponse, deprecated=True)
async def run_match(req: MatchRequest, current_user: dict = Depends(get_current_user)) -> MatchResponse:
    """Execute the full job matching pipeline (legacy alias of ``POST /api/v1/match``).

    Accepts job URLs and resume data, runs the 8-node LangGraph decision
    graph, and returns the match results.

    ⚠️ 引擎调试入口，保留仅为兼容旧脚本。新集成请用 ``/api/v1/match``，
    后者有 X-Client-Key / demo 配额 / 输入长度三重守门。
    """
    if not req.resume_file_path and not req.resume_text:
        raise AppError(
            "Provide either resume_file_path or resume_text",
            code="VALIDATION_ERROR",
            status_code=422,
        )

    graph = get_graph()
    state = await graph.run(
        job_urls=req.job_urls,
        resume_file_path=req.resume_file_path,
        resume_text=req.resume_text,
        session_id=req.session_id,
        use_llm=req.use_llm,
        use_embedding=req.use_embedding,
    )

    # 归属补写（P0-4 / 决策 D-F）：引擎不感知用户身份，session 归属由 API 层
    # 在运行结束后补写。用「仅认领无主 session」语义，不会覆盖他人归属。
    if state.session_id:
        get_store().set_session_owner_if_unowned(state.session_id, current_user["id"])

    top = state.match_results[0] if state.match_results else None

    return MatchResponse(
        session_id=state.session_id or "",
        status="completed" if state.report else "failed",
        routing_decision=state.routing_decision or "N/A",
        routing_reason=state.routing_reason or "",
        jobs_found=state.crawler_output.total_count if state.crawler_output else 0,
        jobs_matched=len(state.match_results),
        top_score=top.score if top else None,
        top_job=top.job.title if top else None,
        average_score=state.report.average_score if state.report else None,
        errors=list(state.errors),
    )


@app.get("/sessions", response_model=list[SessionSummary], deprecated=True)
async def list_sessions(limit: int = Query(20, ge=1, le=200), current_user: dict = Depends(get_current_user)) -> list[SessionSummary]:
    """List recent sessions with basic stats (legacy alias of ``GET /api/v1/sessions``)."""
    store = get_store()
    rows = store.list_sessions(limit=limit, user_id=str(current_user["id"]))
    return [
        SessionSummary(
            session_id=r["session_id"],
            status=r["status"],
            jobs_found=r["config"].get("job_urls", 0),
            jobs_matched=0,
            created_at=r["created_at"],
            updated_at=r["updated_at"],
            error_count=r["error_count"],
        )
        for r in rows
    ]


@app.get("/sessions/{session_id}", deprecated=True)
async def get_session(session_id: str, current_user: dict = Depends(get_current_user)) -> JSONResponse:
    """Get full session details (legacy alias of ``GET /api/v1/sessions/{id}``)."""
    store = get_store()
    # 归属校验先行（P0-4 / 决策 D-G）：归属不符与「不存在」返回同一个 404。
    info = store.get_session_info(session_id, user_id=str(current_user["id"]))

    if not info:
        # raises 404 NOT_FOUND，与其它端点同形（不再裸抛 HTTPException）
        raise ResourceNotFound(f"Session {session_id} not found")

    state = store.load_state(session_id)

    # Match results summary
    results_summary: list[dict[str, Any]] = []
    if state.get("match_results"):
        for mr in state["match_results"]:
            results_summary.append({
                "title": mr.job.title,
                "company": mr.job.company,
                "score": mr.score,
                "grade": mr.grade(),
                "match_reasons": mr.match_reasons,
                "missing_skills": mr.missing_skills,
                "skill_overlap": mr.skill_overlap,
            })

    return JSONResponse({
        "session_id": session_id,
        "status": info["status"],
        "created_at": info["created_at"],
        "updated_at": info["updated_at"],
        "errors": info["errors"],
        "jobs_count": len(state.get("jobs", {}).jobs) if state.get("jobs") else 0,
        "resume_name": state.get("resume", {}).name if state.get("resume") else None,
        "match_results": results_summary,
        "logs": state.get("logs", []),
    })


@app.get("/sessions/{session_id}/report", response_model=None, deprecated=True)
async def download_report(
    session_id: str,
    fmt: str = Query("json", description="Output format: csv, xlsx, md, json"),
    current_user: dict = Depends(get_current_user),
) -> FileResponse | JSONResponse:
    """Download session report (legacy alias of ``GET /api/v1/sessions/{id}/report``)."""
    supported = {"csv", "xlsx", "md", "json"}
    if fmt not in supported:
        raise AppError(
            f"Unsupported format '{fmt}'. Supported: {', '.join(sorted(supported))}",
            code="VALIDATION_ERROR",
            status_code=400,
        )

    # 归属校验（P0-4）：报告文件按 session 命名，先确认该 session 属于调用者。
    if not get_store().get_session_info(session_id, user_id=str(current_user["id"])):
        raise ResourceNotFound(f"Session {session_id} not found")

    output_dir = ROOT_DATA_DIR / "outputs"
    report_file = output_dir / f"report_{session_id}.{fmt}"

    if not report_file.exists():
        raise ResourceNotFound(
            f"Report for session {session_id} not found. Run /match first."
        )

    media_types = {
        "csv": "text/csv",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "md": "text/markdown",
        "json": "application/json",
    }

    return FileResponse(
        path=report_file,
        media_type=media_types[fmt],
        filename=f"job_match_report_{session_id}.{fmt}",
    )


# ── Direct run ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.api.server:app", host="0.0.0.0", port=8000, reload=True)
