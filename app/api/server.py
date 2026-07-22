"""FastAPI REST server for the Job Matching AI Agent.

Provides a production-grade HTTP API wrapping the LangGraph pipeline.

启动:
    uvicorn app.api.server:app --reload --port 8000
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import time

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.core.orchestrator import DecisionGraph
from app.core.state_store import StateStore
from app.utils.logger import logger

# ── App setup ─────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Job Matching AI Agent API",
    description="Multi-agent pipeline: crawl jobs → parse resume → 2-stage matching → ranking report",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Mount versioned API v1 ──────────────────────────────────────────────────
from app.api.router_v1 import router as v1_router
app.include_router(v1_router)

# ── Mount App API ──────────────────────────────────────────────────────────
from app.api.router_app import router as app_router
app.include_router(app_router)

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
            "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"},
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


@app.get("/health")
async def health() -> dict:
    """Health check endpoint."""
    return {
        "status": "ok",
        "service": "job-matching-ai-agent",
        "version": "1.0.0",
    }


@app.post("/match", response_model=MatchResponse)
async def run_match(req: MatchRequest) -> MatchResponse:
    """Execute the full job matching pipeline.

    Accepts job URLs and resume data, runs the 8-node LangGraph decision
    graph, and returns the match results.
    """
    if not req.resume_file_path and not req.resume_text:
        raise HTTPException(
            status_code=422,
            detail="Provide either resume_file_path or resume_text",
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


@app.get("/sessions", response_model=list[SessionSummary])
async def list_sessions(limit: int = Query(20, ge=1, le=200)) -> list[SessionSummary]:
    """List recent sessions with basic stats."""
    store = get_store()
    rows = store.list_sessions(limit=limit)
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


@app.get("/sessions/{session_id}")
async def get_session(session_id: str) -> JSONResponse:
    """Get full session details including jobs, resume, and match results."""
    store = get_store()
    state = store.load_state(session_id)
    info = store.get_session_info(session_id)

    if not info:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

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


@app.get("/sessions/{session_id}/report", response_model=None)
async def download_report(
    session_id: str,
    fmt: str = Query("json", description="Output format: csv, xlsx, md, json"),
) -> FileResponse | JSONResponse:
    """Download session report in specified format."""
    supported = {"csv", "xlsx", "md", "json"}
    if fmt not in supported:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported format '{fmt}'. Supported: {', '.join(sorted(supported))}",
        )

    output_dir = Path(__file__).resolve().parent.parent.parent / "data" / "outputs"
    report_file = output_dir / f"report_{session_id}.{fmt}"

    if not report_file.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Report for session {session_id} not found. Run /match first.",
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
