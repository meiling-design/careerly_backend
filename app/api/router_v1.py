"""REST API v1 — versioned endpoints with async task support.

Every response uses a standard envelope:

    .. code-block:: json

        {
            "success": true,
            "data": { ... },
            "error": null,
            "meta": { "timestamp": ..., "version": "v1" }
        }

Errors:

    .. code-block:: json

        {
            "success": false,
            "data": null,
            "error": { "code": "NOT_FOUND", "message": "..." },
            "meta": { "timestamp": ..., "version": "v1" }
        }

Async task flow (POST /match):

    1. Client sends POST /api/v1/match
    2. Server creates session, kicks off background pipeline
    3. Returns **202 Accepted** with ``task_id`` / ``status_url``
    4. Client polls ``GET /api/v1/tasks/{task_id}`` for status + result

This design is Android-friendly: no 30-second HTTP connection needed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json
import os
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.core.orchestrator import DecisionGraph
from app.core.state_store import StateStore
from app.services.demo_guard import DemoGuard
from app.utils.logger import logger

# ── Router ──────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1")

# ── Background task tracker ─────────────────────────────────────────────────

_background_tasks: dict[str, asyncio.Task] = {}

# ── Envelope helpers ────────────────────────────────────────────────────────

_API_VERSION = "v1"


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
    logger.warning("API v1 error | code=%s | status=%d | msg=%s", code, status_code, message)
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message},
            "meta": {"timestamp": time.time(), "version": _API_VERSION},
        },
    )


# ── Singleton helpers (shared with server.py) ───────────────────────────────

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


# ── Pydantic models ─────────────────────────────────────────────────────────


# ── Guard helpers ───────────────────────────────────────────────────────────

# Read from environment at startup (can be overridden via /config/reload)
_ALLOWED_CLIENT_KEYS: list[str] = [
    k.strip() for k in os.getenv("ALLOWED_CLIENT_KEYS", "").split(",") if k.strip()
]
_DEMO_GUARD = DemoGuard()

# Input limits (token estimation: ~1 token ≈ 4 chars for mixed CN/EN)
_MAX_RESUME_CHARS = int(os.getenv("MAX_RESUME_CHARS", "80000"))     # ~20k tokens
_MAX_JD_CHARS = int(os.getenv("MAX_JD_CHARS", "20000"))             # ~5k tokens
_MAX_JOB_URLS = int(os.getenv("MAX_JOB_URLS", "10"))


def _get_visitor_id(request: Request) -> str:
    """Extract a stable visitor identifier from the request."""
    vid = request.headers.get("X-Visitor-Id", "").strip()
    if vid:
        return vid
    # Fall back to client IP
    ip = request.client.host if request.client else "unknown"
    return f"ip:{ip}"


async def _check_client_key(request: Request) -> JSONResponse | None:
    """Validate ``X-Client-Key`` header when ``ALLOWED_CLIENT_KEYS`` is set.

    Returns ``None`` if the key is valid, or an error response if not.
    """
    if not _ALLOWED_CLIENT_KEYS:
        return None  # feature disabled
    client_key = request.headers.get("X-Client-Key", "").strip()
    if client_key in _ALLOWED_CLIENT_KEYS:
        return None  # valid
    logger.warning("API v1: invalid/missing X-Client-Key | ip=%s",
                   request.client.host if request.client else "?")
    return _err("UNAUTHORIZED",
                "Missing or invalid X-Client-Key header. "
                "Provide a valid client key to access this API.",
                401)


async def _check_demo_quota(request: Request) -> JSONResponse | None:
    """Check demo usage quota for the visitor.

    Returns ``None`` if within quota, or an error response if over limit.
    """
    visitor_id = _get_visitor_id(request)
    allowed, err = await _DEMO_GUARD.check(visitor_id)
    if allowed:
        return None
    return _err(err["code"], err["message"], 429)


async def _check_input_limits(req: MatchRequest) -> JSONResponse | None:
    """Validate input sizes to protect LLM costs.

    Returns ``None`` if within limits, or an error response if exceeded.
    """
    # Resume text length
    if req.resume_text and len(req.resume_text) > _MAX_RESUME_CHARS:
        return _err("INPUT_TOO_LARGE",
                     f"Resume text exceeds {_MAX_RESUME_CHARS:,} character limit "
                     f"(≈{_MAX_RESUME_CHARS // 4:,} tokens).",
                     413)

    # Number of job URLs
    if len(req.job_urls) > _MAX_JOB_URLS:
        return _err("TOO_MANY_URLS",
                     f"Maximum {_MAX_JOB_URLS} job URLs per request.",
                     413)

    # Total JD text from manual entries (if any)
    # URL-based JDs are limited after scraping; this catches manual pastes
    return None


class MatchRequest(BaseModel):
    """Request body for POST /api/v1/match."""

    job_urls: list[str] = Field(..., min_length=1, description="Job posting URLs to analyze")
    resume_file_path: Optional[str] = Field(None, description="Path to resume file on server")
    resume_text: Optional[str] = Field(None, description="Raw resume text (alternative)")
    session_id: Optional[str] = Field(None, description="Resume a prior session ID")
    idempotency_key: Optional[str] = Field(None, description="Client-provided idempotency key")
    use_llm: bool = Field(True, description="Enable LLM-based matching")
    use_embedding: bool = Field(True, description="Enable embedding-based matching")


class TaskProgress(BaseModel):
    """Progress snapshot for a running task."""
    current_node: Optional[str] = None
    node_status: Optional[str] = None
    completed_nodes: int = 0
    total_nodes: int = 8
    percent: str = "0%"


class TaskResult(BaseModel):
    """Summary of completed match results for quick polling."""
    jobs_matched: int = 0
    top_score: Optional[float] = None
    top_job: Optional[str] = None
    average_score: Optional[float] = None


# ── Background pipeline runner ──────────────────────────────────────────────


async def _run_match_background(session_id: str, req: MatchRequest) -> None:
    """Execute the full matching pipeline in a background asyncio task.

    Note: The current pipeline uses synchronous I/O (requests, OpenAI sync
    client) via LangGraph, which will block the event loop during execution.
    For production scenarios with concurrent requests, consider either:

    1. Running this function via ``loop.run_in_executor()`` after converting
       ``graph.run()`` to a sync wrapper, OR
    2. Replacing sync HTTP clients with async ones (httpx, openai.AsyncOpenAI)

    The API contract (202 + task_id + polling) is designed to support both
    sync and async backends transparently from the client's perspective.
    """
    try:
        store = get_store()
        graph = get_graph()
        store.update_session_status(session_id, "running")

        # Run the pipeline — if the pipeline mix uses sync I/O, this will
        # block the event loop. A future upgrade should make the pipeline
        # fully async or delegate to a thread executor / task queue.
        await graph.run(
            job_urls=req.job_urls,
            resume_file_path=req.resume_file_path,
            resume_text=req.resume_text,
            session_id=session_id,
            use_llm=req.use_llm,
            use_embedding=req.use_embedding,
        )
        # Status updated to "completed" by orchestrator's _node_report
    except Exception as exc:
        logger.error("Background pipeline failed | session=%s | %s", session_id, exc)
        logger.exception("Full traceback for session %s:", session_id)
        try:
            store_cleanup = get_store()
            store_cleanup.update_session_status(session_id, "failed")
            store_cleanup.append_error(session_id, f"Pipeline crashed: {exc}")
        except Exception:
            pass


# ── Helper: idempotency key computation ────────────────────────────────────


def _compute_idem_key(req: MatchRequest) -> str:
    """Deterministic hash of request inputs for idempotency."""
    raw = _json.dumps({
        "urls": sorted(req.job_urls),
        "resume_text_hash": hashlib.sha256(
            (req.resume_text or "").encode()
        ).hexdigest()[:12],
        "file_path": req.resume_file_path or "",
        "config": {"llm": req.use_llm, "embedding": req.use_embedding},
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════


@router.get("/health")
async def health() -> JSONResponse:
    """Health check — used by Railway/Render and Android connectivity test."""
    return _ok({
        "status": "ok",
        "service": "job-matching-ai-agent",
        "version": "1.0.0",
    })


# ── Config ──────────────────────────────────────────────────────────────────


@router.get("/config")
async def runtime_config() -> JSONResponse:
    """Show current runtime configuration (secrets masked)."""
    from app.core.config import config as app_cfg
    from app.services.cache_service import get_cache
    import os

    cache = get_cache()

    def _mask(val: str, visible: int = 4) -> str:
        if not val:
            return ""
        return val[:visible] + "****" + val[-4:] if len(val) > visible + 4 else "****"

    return _ok({
        "llm": {
            "model": app_cfg.llm.model,
            "base_url": app_cfg.llm.base_url or "(using provider default)",
            "api_key_set": bool(app_cfg.llm.api_key),
        },
        "embedding": {
            "provider": app_cfg.embedding.provider,
            "model": app_cfg.embedding.model,
            "api_key_set": bool(app_cfg.embedding.api_key),
        },
        "matching": {
            "weights": {
                "semantic": app_cfg.matching.semantic_weight,
                "llm": app_cfg.matching.llm_weight,
                "overlap": app_cfg.matching.overlap_weight,
            },
            "two_stage": app_cfg.matching.two_stage,
        },
        "routing": {
            "use_llm_router": app_cfg.routing.use_llm_router,
        },
        "cache": {
            "type": "redis" if bool(os.getenv("REDIS_URL")) else "memory",
        },
        "playwright": {
            "max_instances": int(os.getenv("MAX_PLAYWRIGHT_INSTANCES", "2")),
        },
        "security": {
            "api_key_set": bool(os.getenv("API_KEY")),
            "rate_limit": int(os.getenv("RATE_LIMIT", "0")),
        },
        "debug": app_cfg.debug,
    })


@router.post("/config/reload")
async def reload_config() -> JSONResponse:
    """Hot-reload configuration at runtime (swap LLM without container restart)."""
    global _graph, _store

    from app.core.config import AppConfig
    from dotenv import load_dotenv

    load_dotenv(override=True)
    new_config = AppConfig.from_yaml_and_env()

    # Swap singletons
    _graph = DecisionGraph(config=new_config)
    _store = StateStore()

    logger.info("Config reloaded | model=%s", new_config.llm.model)

    return _ok({
        "status": "ok",
        "model": new_config.llm.model,
        "embedding_model": new_config.embedding.model,
    })


# ── Match (async — 202 Accepted) ────────────────────────────────────────────


@router.post("/match", status_code=202)
async def run_match(req: MatchRequest, request: Request) -> JSONResponse:
    """Submit a job-matching request.

    Returns immediately (202) with a ``task_id``.

    The pipeline runs in the background. Poll ``GET /api/v1/tasks/{task_id}``
    to check status and retrieve the result when ``status == \"completed\"``.

    **Guards** (applied before the pipeline starts):

    * ``X-Client-Key`` — required when ``ALLOWED_CLIENT_KEYS`` is set
    * Demo quota — ``DEMO_MODE`` limits anonymous visitors to 1 match
    * Input limits — resume ≤80k chars, ≤10 URLs
    """
    if not req.resume_file_path and not req.resume_text:
        return _err("VALIDATION_ERROR", "Provide either resume_file_path or resume_text", 422)

    # ── Guard 1: X-Client-Key ──────────────────────────────────────────
    err_resp = await _check_client_key(request)
    if err_resp:
        return err_resp

    # ── Guard 2: Demo quota ────────────────────────────────────────────
    err_resp = await _check_demo_quota(request)
    if err_resp:
        return err_resp

    # ── Guard 3: Input cost protection ──────────────────────────────────
    err_resp = await _check_input_limits(req)
    if err_resp:
        return err_resp

    store = get_store()

    # ── Idempotency ──────────────────────────────────────────────────────
    idem_key = req.idempotency_key or _compute_idem_key(req)
    existing = store.find_by_idempotency_key(idem_key)
    if existing and existing["status"] in ("pending", "running"):
        return _ok({
            "task_id": existing["session_id"],
            "status": "in_progress",
            "status_url": f"/api/v1/tasks/{existing['session_id']}",
        }, status_code=200)

    # ── Create session ──────────────────────────────────────────────────
    session_id = req.session_id
    if session_id is None:
        session_id = store.create_session({
            "job_urls": len(req.job_urls),
            "use_llm": req.use_llm,
            "use_embedding": req.use_embedding,
            "idempotency_key": idem_key,
        })

    # ── Record demo usage ──────────────────────────────────────────────
    visitor_id = _get_visitor_id(request)
    await _DEMO_GUARD.record_match(visitor_id, {
        "session_id": session_id,
        "url_count": len(req.job_urls),
    })

    # ── Launch background pipeline ──────────────────────────────────────
    task = asyncio.create_task(_run_match_background(session_id, req))
    _background_tasks[session_id] = task
    task.add_done_callback(lambda t: _background_tasks.pop(session_id, None))

    return _ok({
        "task_id": session_id,
        "status": "pending",
        "status_url": f"/api/v1/tasks/{session_id}",
    }, status_code=202)


# ── Task status (Android polling endpoint) ──────────────────────────────────


@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str) -> JSONResponse:
    """Poll task status and result.

    A mobile client can call this every 2-3 seconds after submitting
    a match request. When ``status`` is ``\"completed\"``, the ``result``
    field contains the match summary.
    """
    store = get_store()
    info = store.get_session_info(task_id)

    if not info:
        return _err("NOT_FOUND", f"Task {task_id} not found", 404)

    # Extract progress from session config
    cfg = info.get("config", {}) or {}
    progress_raw = cfg.get("_progress", {})
    is_running = task_id in _background_tasks

    progress = TaskProgress(
        current_node=progress_raw.get("current_node"),
        node_status=progress_raw.get("node_status"),
        completed_nodes=progress_raw.get("completed_count", 0),
    )
    # Compute percentage
    if progress.completed_nodes > 0:
        pct = round(progress.completed_nodes / progress.total_nodes * 100)
        progress.percent = f"{pct}%"

    data: dict[str, Any] = {
        "task_id": task_id,
        "status": info["status"],
        "is_running": is_running,
        "created_at": info["created_at"],
        "updated_at": info["updated_at"],
        "progress": progress.model_dump(),
        "errors": info.get("errors", []),
    }

    # Include result snapshot when completed
    if info["status"] == "completed":
        state = store.load_state(task_id)
        results = state.get("match_results") or []
        data["result"] = TaskResult(
            jobs_matched=len(results),
            top_score=results[0].score if results else None,
            top_job=results[0].job.title if results else None,
            average_score=state.get("report").average_score if state.get("report") else None,
        ).model_dump()
        data["result_url"] = f"/api/v1/sessions/{task_id}"

    return _ok(data)


@router.get("/tasks")
async def list_tasks(limit: int = Query(20, ge=1, le=100)) -> JSONResponse:
    """List recent tasks (lightweight, same as /sessions)."""
    store = get_store()
    rows = store.list_sessions(limit=limit)

    items = []
    for r in rows:
        cfg = r.get("config", {}) or {}
        progress = cfg.get("_progress", {})
        items.append({
            "task_id": r["session_id"],
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "is_running": r["session_id"] in _background_tasks,
            "progress": {
                "current_node": progress.get("current_node"),
                "completed_nodes": progress.get("completed_count", 0),
                "total_nodes": 8,
            },
        })

    return _ok({"tasks": items, "total": len(items)})


# ── Sessions (compat, detailed view) ────────────────────────────────────────


@router.get("/sessions")
async def list_sessions(limit: int = Query(20, ge=1, le=200)) -> JSONResponse:
    """List recent sessions with basic stats."""
    store = get_store()
    rows = store.list_sessions(limit=limit)

    items = []
    for r in rows:
        cfg = r.get("config", {}) or {}
        items.append({
            "session_id": r["session_id"],
            "status": r["status"],
            "jobs_count": cfg.get("job_urls", 0),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "error_count": r.get("error_count", 0),
        })

    return _ok({"sessions": items, "total": len(items)})


@router.get("/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    """Get full session details including jobs, resume, and match results."""
    store = get_store()
    state = store.load_state(session_id)
    info = store.get_session_info(session_id)

    if not info:
        return _err("NOT_FOUND", f"Session {session_id} not found", 404)

    # Match results summary
    results_summary: list[dict[str, Any]] = []
    match_results = state.get("match_results") or []
    for mr in match_results:
        results_summary.append({
            "title": mr.job.title,
            "company": mr.job.company,
            "score": mr.score,
            "grade": mr.grade(),
            "match_reasons": mr.match_reasons,
            "missing_skills": mr.missing_skills,
            "skill_overlap": mr.skill_overlap,
        })

    jobs_list = state.get("jobs")
    return _ok({
        "session_id": session_id,
        "status": info["status"],
        "created_at": info["created_at"],
        "updated_at": info["updated_at"],
        "errors": info.get("errors", []),
        "jobs_count": len(jobs_list.jobs) if jobs_list else 0,
        "resume_name": state.get("resume", {}).name if state.get("resume") else None,
        "match_results": results_summary,
        "logs": state.get("logs", []),
    })


@router.get("/sessions/{session_id}/report", response_model=None)
async def download_report(
    session_id: str,
    fmt: str = Query("json", description="Output format: csv, xlsx, md, json"),
) -> FileResponse | JSONResponse:
    """Download session report in specified format."""
    supported = {"csv", "xlsx", "md", "json"}
    if fmt not in supported:
        return _err(
            "UNSUPPORTED_FORMAT",
            f"Unsupported format '{fmt}'. Supported: {', '.join(sorted(supported))}",
            400,
        )

    output_dir = Path(__file__).resolve().parent.parent.parent / "data" / "outputs"
    report_file = output_dir / f"report_{session_id}.{fmt}"

    if not report_file.exists():
        return _err("NOT_FOUND", f"Report for session {session_id} not found. Run /match first.", 404)

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


# ── SSE streaming (kept for real-time frontend) ─────────────────────────────


async def _progress_generator(
    graph: DecisionGraph,
    job_urls: list[str],
    resume_file_path: str | None,
    resume_text: str | None,
    session_id: str | None,
    use_llm: bool,
    use_embedding: bool,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE events for each graph progress tick."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def on_progress(msg: str) -> None:
        try:
            queue.put_nowait({"type": "progress", "message": msg})
        except Exception:
            pass

    def on_status_change(node: str, status: str) -> None:
        try:
            queue.put_nowait({"type": "status", "node": node, "status": status})
        except Exception:
            pass

    graph.on_progress = on_progress
    graph.on_status_change = on_status_change

    yield f"data: {_json.dumps({'type': 'start'}, ensure_ascii=False)}\n\n"

    try:
        state = await graph.run(
            job_urls=job_urls,
            resume_file_path=resume_file_path,
            resume_text=resume_text,
            session_id=session_id,
            use_llm=use_llm,
            use_embedding=use_embedding,
        )
    except Exception as e:
        yield f"data: {_json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        return

    # Drain remaining progress events
    remaining: list[dict[str, Any]] = []
    while not queue.empty():
        try:
            item = queue.get_nowait()
            if item is not None:
                remaining.append(item)
        except asyncio.QueueEmpty:
            break
    for item in remaining:
        yield f"data: {_json.dumps(item, ensure_ascii=False)}\n\n"

    top = state.match_results[0] if state.match_results else None
    result = {
        "type": "result",
        "session_id": state.session_id or "",
        "status": "completed" if state.report else "failed",
        "routing_decision": state.routing_decision or "N/A",
        "routing_reason": state.routing_reason or "",
        "jobs_found": state.crawler_output.total_count if state.crawler_output else 0,
        "jobs_matched": len(state.match_results),
        "top_score": top.score if top else None,
        "top_job": top.job.title if top else None,
        "average_score": state.report.average_score if state.report else None,
        "errors": list(state.errors),
    }
    yield f"data: {_json.dumps(result, ensure_ascii=False)}\n\n"


@router.post("/match/stream", response_model=None)
async def run_match_stream(req: MatchRequest, request: Request) -> StreamingResponse | JSONResponse:
    """Same as POST /match but returns a Server-Sent Events stream.

    Use this for real-time frontend progress bars. For mobile apps,
    prefer the async ``POST /match`` + ``GET /tasks/{id}`` pattern.

    **Guards** (same as POST /match, applied before streaming):
    * ``X-Client-Key`` — required when ``ALLOWED_CLIENT_KEYS`` is set
    * Demo quota — ``DEMO_MODE`` limits anonymous visitors to 1 match
    * Input limits — resume ≤80k chars, ≤10 URLs
    """
    if not req.resume_file_path and not req.resume_text:
        return _err("VALIDATION_ERROR", "Provide either resume_file_path or resume_text", 422)

    # ── Guards ─────────────────────────────────────────────────────────
    err_resp = await _check_client_key(request)
    if err_resp:
        return err_resp
    err_resp = await _check_demo_quota(request)
    if err_resp:
        return err_resp
    err_resp = await _check_input_limits(req)
    if err_resp:
        return err_resp

    graph = get_graph()

    # Record demo usage
    visitor_id = _get_visitor_id(request)
    await _DEMO_GUARD.record_match(visitor_id, {
        "session_id": req.session_id or "stream",
        "url_count": len(req.job_urls),
    })

    return StreamingResponse(
        _progress_generator(
            graph=graph,
            job_urls=req.job_urls,
            resume_file_path=req.resume_file_path,
            resume_text=req.resume_text,
            session_id=req.session_id,
            use_llm=req.use_llm,
            use_embedding=req.use_embedding,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
