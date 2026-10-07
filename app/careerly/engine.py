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
from typing import Any, Optional

from app.careerly.schemas import (
    AnalysisProject,
    AnalysisResult,
    GapDetail,
    JobMatch,
    JobModel,
    StrengthScore,
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


def _score_capabilities_safe(resume: Any, raw_jd: str) -> Optional[list[dict]]:
    """C4: one LLM call for capability scores; any failure degrades to None.

    Never raises — a scoring failure must not break the analysis (decision:
    LLM 评分 + 失败降级).
    """
    try:
        from app.careerly.capability import normalize
        from app.services.llm_service import LLMService

        profile = resume.model_dump() if hasattr(resume, "model_dump") else dict(resume)
        raw = LLMService().score_capabilities(profile, raw_jd)
        caps = normalize(raw)
        if caps:
            logger.info("Careerly capability scoring OK | %d dimensions", len(caps))
        return caps
    except Exception as exc:  # pragma: no cover - defensive: LLM/network failures
        logger.warning("Careerly capability scoring skipped: %s", exc)
        return None


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
    capabilities: Optional[list[dict]] = None,
) -> AnalysisProject:
    """Assemble a version-1 ``AnalysisProject`` from engine results.

    ``capabilities`` (C4, optional) is the validated output of
    :mod:`app.careerly.capability` — when present it fills
    ``strengthScores`` / ``gapDetails`` (with yours/target/why/actions) and
    ``capabilitySnapshot``; otherwise those stay at the pre-C4 defaults
    (empty / null / {}).
    """
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
        capabilitySnapshot={},  # C4 — filled when capability scoring succeeded
        createdAt=iso(now),
    )

    if capabilities:
        _apply_capabilities(result, capabilities)

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


# Gap "distance" threshold: target - yours >= this → 关键 Gap, else 一般 Gap
_KEY_GAP_DELTA = 12
# A capability only counts as a "strength" at/above this score (demo fixture
# shows the weakest dimension excluded from strengthScores)
_STRENGTH_FLOOR = 60


def _apply_capabilities(result: AnalysisResult, capabilities: list[dict]) -> None:
    """Fill C4 fields on the single result from validated capability scores."""
    ranked = sorted(capabilities, key=lambda c: c["yours"], reverse=True)

    strengths = [c for c in ranked if c["yours"] >= _STRENGTH_FLOOR][:6]
    snapshot = {c["name"]: c["yours"] for c in capabilities}

    for match in result.matches:
        match.strengthScores = [StrengthScore(name=c["name"], score=c["yours"]) for c in strengths]
        # Product intent (demo fixture L146): strengths mirror strengthScores names
        match.strengths = [s.name for s in match.strengthScores] or match.strengths
        match.gapDetails = [
            GapDetail(
                name=c["name"],
                yours=c["yours"],
                target=c["target"],
                level="关键 Gap" if c["target"] - c["yours"] >= _KEY_GAP_DELTA else "一般 Gap",
                why=c["why"],
                actions=c["actions"],
            )
            for c in ranked
        ]
        if match.gapDetails:
            top_actions = match.gapDetails[0].actions
            match.suggestion = match.suggestion or (top_actions[0] if top_actions else None)

    result.capabilitySnapshot = snapshot


def run_analysis(
    *,
    title: str,
    raw_jd: str,
    resume_text: str,
    use_llm: bool = True,
    use_embedding: bool = True,
    session_id: str | None = None,
) -> tuple[AnalysisProject, dict]:
    """Run the matching engine for one pasted JD and map it to a project.

    Returns ``(project, resume_profile)`` where ``resume_profile`` is the
    engine's parsed resume (``ResumeProfile.model_dump()``, possibly ``{}``).

    ⚠️ **Why the profile is returned instead of dropped (P3-1)**
    -----------------------------------------------------------
    Before P3 the parsed resume was used for capability scoring and then
    discarded. But ``GET /api/v1/career/assets`` needs a *real* source for
    work experiences / projects / skills — and re-parsing the resume on every
    assets request would mean an LLM call per page view.

    The parse already happens here, for free, inside the analysis the user
    paid for. Persisting it is the difference between "职业资产 has a real
    data source" and "职业资产 是 Mock".

    Returning a tuple (rather than hanging the profile off ``AnalysisProject``)
    keeps the wire contract untouched: ``resumeProfile`` is *not* a field of
    ``AnalysisProject`` in ``schemas.py``, and adding one would change the
    JSON the Flutter client already parses.

    Synchronous on purpose: the caller offloads it to a thread executor so the
    event loop is not blocked (see ``app/careerly/router.py``).
    """
    from app.core.orchestrator import DecisionGraph

    if not resume_text or not resume_text.strip():
        # P1-2：语义从「缺少一份文件」改为「尚未建立职业基线」——
        # 后者才是 Careerly 的产品语言（基线 = 后续所有 Gap / 成长的参照系）。
        raise AnalysisError(
            "NO_BASELINE",
            "尚未建立职业基线：请先上传简历或传入 resumeText",
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
            session_id=session_id,
        )
    except Exception as exc:  # pragma: no cover - engine-level failure
        logger.exception("Careerly analysis failed: %s", exc)
        raise AnalysisError("ANALYSIS_FAILED", f"分析失败：{exc}", 502) from exc

    # P1-2：RoutingNode 判定「数据不足、需人工介入」时，图会直接走向 END
    # （orchestrator.py:157 "ask_user" -> END），此时 match_results 为空。
    # 这**不是**系统故障，不能和 ANALYSIS_EMPTY 混为一谈 —— 前端需要据此
    # 提示用户补充信息，而不是显示「分析失败」。
    if getattr(state, "routing_decision", "") == "ask_user":
        reason = (getattr(state, "routing_reason", "") or "").strip()
        raise AnalysisError(
            "NEED_MORE_INPUT",
            reason or "信息不足，无法完成分析：请补充更完整的岗位描述或简历内容",
            422,
        )

    results: list[MatchResult] = list(state.match_results or [])
    if not results:
        detail = "；".join(state.errors) if state.errors else "引擎未返回匹配结果"
        raise AnalysisError("ANALYSIS_EMPTY", f"未能产出匹配结果：{detail}", 422)

    # C4：能力分值。resume 画像失败不影响分析结果本身（详见
    # ``_score_capabilities_safe`` 的降级策略）。
    #
    # ⚠️ 字段名必须是 ``resume_output`` —— ``GraphState`` 上没有 ``resume``。
    # 写成 ``state.resume`` 会抛 AttributeError，而这个调用**不在**上面那个
    # try 块里，于是它会一路冒到 router 的兜底分支，把一次**已经成功**的
    # 分析变成 502。这个 bug 潜伏到 P1-5 才被发现，正因为没有测试真的跑引擎。
    resume_profile = getattr(state, "resume_output", None)
    capabilities = _score_capabilities_safe(resume_profile, raw_jd) if resume_profile else None
    project = build_project(
        title=title, raw_jd=raw_jd, match_results=results, capabilities=capabilities,
    )
    # P3-1：把画像序列化后交给调用方持久化（职业资产的真实数据源）。
    # 失败不得影响分析 —— 与成长记录同一纪律。
    try:
        profile_dump: dict = (
            resume_profile.model_dump() if hasattr(resume_profile, "model_dump") else {}
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("Careerly resume profile serialisation failed")
        profile_dump = {}
    logger.info(
        "Careerly analysis done | %.1fs | session=%s | score=%s",
        time.time() - started,
        state.session_id,
        project.results[0].matches[0].score if project.results and project.results[0].matches else None,
    )
    return project, profile_dump
