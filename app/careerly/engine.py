"""Bridge between the job-matching engine and the Careerly contract.

The engine (``DecisionGraph`` / ``MatchingAgent``) produces ``MatchResult``
objects whose shape is *not* the Careerly wire format.  This module owns the
translation, plus the one rule the engine cannot know: **the job title comes
from the user's request**, not from the engine.

⚠️ Why the title must be taken from the request
-----------------------------------------------
``DecisionGraph._node_crawl`` hard-codes ``title="[手动输入] 职位 #N"`` for
pasted JDs, so the engine's own title is unusable for Careerly's
``analyze({title, rawJd})``.  The engine is therefore used **only for
scoring / gap analysis**; ``Job`` is built from the request.
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from app.careerly.schemas import (
    AnalysisProject,
    AnalysisResult,
    GapDetail,
    JobMatch,
    JobModel,
    iso,
)
from app.models.match import MatchResult
from app.utils.logger import logger

# SkillGap.severity -> Careerly GapDetail.level
_LEVEL = {
    "missing": "关键 Gap",
    "gap": "关键 Gap",
    "weak": "一般 Gap",
}


class AnalysisError(Exception):
    """Domain error surfaced to the client as an error envelope."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def to_job_match(result: MatchResult, job_id: str, rank: int) -> JobMatch:
    """Map one engine ``MatchResult`` onto Careerly's ``JobMatch``."""
    # gaps: missing + partial, de-duplicated but order-preserving
    gaps: list[str] = []
    for skill in list(result.missing_skills) + list(result.partial_skills):
        if skill and skill not in gaps:
            gaps.append(skill)

    gap_details = [
        GapDetail(
            name=g.skill,
            # your/target stay null until the capability score model exists (C4)
            yours=None,
            target=None,
            level=_LEVEL.get(g.severity, "一般 Gap"),
            why=None,
            actions=[g.suggestion] if g.suggestion else [],
        )
        for g in result.gaps
    ]

    suggestion: Optional[str] = None
    if result.recommendations:
        suggestion = "；".join(r for r in result.recommendations if r) or None
    elif gap_details and gap_details[0].actions:
        suggestion = gap_details[0].actions[0]

    return JobMatch(
        jobId=job_id,
        score=max(0, min(100, round(result.score * 100))),
        rank=rank,
        strengths=list(result.skill_overlap),
        gaps=gaps,
        reasons=list(result.match_reasons),
        suggestion=suggestion,
        strengthScores=[],   # needs per-capability scores (C4) — intentionally empty
        gapDetails=gap_details,
    )


def build_project(
    *,
    title: str,
    raw_jd: str,
    match_results: list[MatchResult],
    source: str = "paste",
) -> AnalysisProject:
    """Assemble a version-1 ``AnalysisProject`` from engine results."""
    now = time.time()
    job_id = _new_id("job")
    job = JobModel(
        id=job_id,
        title=title.strip(),
        company="",           # request has no company field (D85-era contract)
        rawJd=raw_jd,
        source=source,
        createdAt=iso(now),
    )

    ordered = sorted(match_results, key=lambda r: r.score, reverse=True)
    matches = [
        to_job_match(r, job_id=job_id, rank=i + 1)
        for i, r in enumerate(ordered)
    ]

    result = AnalysisResult(
        id=_new_id("res"),
        projectId="",  # filled in below
        version=1,
        resumeVersionId=None,
        matches=matches,
        capabilitySnapshot={},  # C4 — no capability score model yet
        createdAt=iso(now),
    )

    project_id = _new_id("proj")
    result.projectId = project_id

    return AnalysisProject(
        id=project_id,
        name=title.strip(),
        jobs=[job],
        results=[result],
        createdAt=iso(now),
        updatedAt=iso(now),
    )


def run_analysis(
    *,
    title: str,
    raw_jd: str,
    resume_text: str,
    use_llm: bool = True,
    use_embedding: bool = True,
) -> AnalysisProject:
    """Run the matching engine for one pasted JD and map it to a project.

    Synchronous on purpose: the caller offloads it to a thread executor so the
    event loop is not blocked (see ``app/careerly/router.py``).
    """
    from app.core.orchestrator import DecisionGraph

    if not resume_text or not resume_text.strip():
        raise AnalysisError(
            "NO_RESUME",
            "尚未建立职业基线（缺少简历文本），无法进行岗位分析",
            422,
        )

    graph = DecisionGraph()
    started = time.time()

    try:
        state = graph.run_sync(
            job_urls=[],
            resume_text=resume_text,
            manual_job_texts=[raw_jd],
            use_llm=use_llm,
            use_embedding=use_embedding,
        )
    except Exception as exc:  # pragma: no cover - engine-level failure
        logger.exception("Careerly analysis failed: %s", exc)
        raise AnalysisError("ANALYSIS_FAILED", f"分析失败：{exc}", 502) from exc

    results: list[MatchResult] = list(state.match_results or [])
    if not results:
        detail = "；".join(state.errors) if state.errors else "引擎未返回匹配结果"
        raise AnalysisError("ANALYSIS_EMPTY", f"未能产出匹配结果：{detail}", 422)

    project = build_project(title=title, raw_jd=raw_jd, match_results=results)
    logger.info(
        "Careerly analysis done | %.1fs | session=%s | score=%s",
        time.time() - started,
        state.session_id,
        project.results[0].matches[0].score if project.results and project.results[0].matches else None,
    )
    return project
