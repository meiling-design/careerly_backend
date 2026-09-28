"""Pydantic models mirroring the Careerly Flutter JSON contract.

⚠️ WHY camelCase FIELD NAMES
---------------------------
Every field name below is copied **verbatim** from the corresponding Dart
model's ``toJson()`` / ``fromJson()`` (see
``G:/ai_agent/careerly/lib/data/models/*.dart``).  Renaming them to Python
style (``raw_jd``, ``created_at`` …) would silently break
``Model.fromJson`` on the client, so the camelCase names are kept on purpose
and are the single source of truth for the wire format.

Timestamps are emitted as ISO-8601 strings because the Dart side parses them
with ``DateTime.tryParse``; ``None`` is emitted as JSON ``null``.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════
#  Analysis — job / match / result / project
# ══════════════════════════════════════════════════════════════════════════


class JobModel(BaseModel):
    """`lib/data/models/job.dart` → ``Job``."""

    id: str
    title: str = ""
    company: str = ""
    rawJd: str = ""
    source: Optional[str] = None
    createdAt: Optional[str] = None


class StrengthScore(BaseModel):
    """`analysis_result.dart` → ``StrengthScore`` (Figma visual extension)."""

    name: str = ""
    score: int = 0


class GapDetail(BaseModel):
    """`analysis_result.dart` → ``GapDetail``.

    ⚠️ ``yours`` / ``target`` are the numeric gap inputs.  The current engine
    has **no per-capability 0–100 score** (unresolved conflict C4), so they
    are emitted as ``null`` and Dart's ``GapDetail.gap`` getter stays ``null``.
    """

    name: str = ""
    yours: Optional[int] = None
    target: Optional[int] = None
    level: Optional[str] = None
    why: Optional[str] = None
    actions: list[str] = Field(default_factory=list)


class JobMatch(BaseModel):
    """`analysis_result.dart` → ``JobMatch``.

    PRD §9.4 hard rule: ``score`` is 0–100 and derived **only** from
    capability matching — salary / commute / city must never feed into it.
    """

    jobId: str
    score: int = 0
    rank: int = 0
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    suggestion: Optional[str] = None
    strengthScores: list[StrengthScore] = Field(default_factory=list)
    gapDetails: list[GapDetail] = Field(default_factory=list)


class AnalysisResult(BaseModel):
    """`analysis_result.dart` → ``AnalysisResult`` (versioned, never overwritten)."""

    id: str
    projectId: str = ""
    version: int = 1
    resumeVersionId: Optional[str] = None
    matches: list[JobMatch] = Field(default_factory=list)
    capabilitySnapshot: dict[str, int] = Field(default_factory=dict)
    createdAt: Optional[str] = None


class AnalysisProject(BaseModel):
    """`analysis_project.dart` → ``AnalysisProject``.

    Container: ``jobs`` (reusable ``Job`` entities) × ``results`` (versions).
    Removing a job from a project must NOT delete the ``Job`` entity.
    """

    id: str
    name: str = ""
    jobs: list[JobModel] = Field(default_factory=list)
    results: list[AnalysisResult] = Field(default_factory=list)
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None


class QuotaState(BaseModel):
    """`quota_state.dart` → ``QuotaState``."""

    total: int = 3
    used: int = 0
    isMember: bool = False


# ══════════════════════════════════════════════════════════════════════════
#  Analysis — request bodies (backend-only, not in the Dart contract)
# ══════════════════════════════════════════════════════════════════════════


class AnalyzeRequest(BaseModel):
    """`AnalysisRepository.analyze({required title, required rawJd})`.

    ``resumeText`` is an **optional backend extension**: the Dart contract has
    no resume parameter (it uses the user's current resume).  Until the resume
    baseline is persisted (phase P3), the text can be supplied inline.
    """

    title: str = Field(..., min_length=1, max_length=200)
    rawJd: str = Field(..., min_length=1)
    resumeText: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════
#  Resume — versions / profile
# ══════════════════════════════════════════════════════════════════════════


class ResumeVersion(BaseModel):
    """`resume_profile.dart` → ``ResumeVersion``."""

    id: str
    label: str = ""
    fileName: Optional[str] = None
    uploadedAt: Optional[str] = None
    isCurrent: bool = False


class ResumeProfile(BaseModel):
    """`resume_profile.dart` → ``ResumeProfile``."""

    versions: list[ResumeVersion] = Field(default_factory=list)
    workExperiences: list[str] = Field(default_factory=list)
    projectExperiences: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    awards: list[str] = Field(default_factory=list)
    capabilities: dict[str, int] = Field(default_factory=dict)
    updatedAt: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════
#  Growth — events / capabilities
# ══════════════════════════════════════════════════════════════════════════


class GrowthEvent(BaseModel):
    """`growth_event.dart` → ``GrowthEvent``.

    ⚠️ ``type`` MUST be the Dart enum's ``.name`` — one of
    ``experience | skill | capability | matchScore | rank``.
    The Chinese labels (新增经历 / 技能变化 / …) are display-only and would
    fail ``GrowthEventType.tryParse``.
    """

    id: str
    type: str = "experience"
    title: str = ""
    description: Optional[str] = None
    delta: Optional[int] = None
    occurredAt: Optional[str] = None


class Capability(BaseModel):
    """`capability.dart` → ``Capability`` (distinct from ``SkillGroup.skills``)."""

    name: str = ""
    score: int = 0
    delta: int = 0
    previousScore: Optional[int] = None


# ══════════════════════════════════════════════════════════════════════════
#  Career assets — the rich "我的" aggregate
# ══════════════════════════════════════════════════════════════════════════


class SkillDelta(BaseModel):
    """`work_experience.dart` → ``SkillDelta`` (inner part of WorkExperience)."""

    name: str = ""
    delta: int = 0


class WorkExperience(BaseModel):
    """`work_experience.dart` → ``WorkExperience``."""

    id: str
    period: str = ""
    title: str = ""
    company: str = ""
    description: str = ""
    skills: list[SkillDelta] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)


class JobImpact(BaseModel):
    """`project_experience.dart` → ``JobImpact``."""

    job: str = ""
    delta: int = 0


class ProjectExperience(BaseModel):
    """`project_experience.dart` → ``ProjectExperience``."""

    id: str
    name: str = ""
    year: str = ""
    type: str = ""
    role: str = ""
    description: str = ""
    outcome: str = ""
    skills: list[str] = Field(default_factory=list)
    jobImpact: list[JobImpact] = Field(default_factory=list)


class SkillGroup(BaseModel):
    """`career_skill.dart` → ``SkillGroup``."""

    category: str = ""
    skills: list[str] = Field(default_factory=list)


class Award(BaseModel):
    """`award.dart` → ``Award``."""

    name: str = ""
    year: str = ""
    org: str = ""
    description: str = ""


class CareerAssets(BaseModel):
    """`career_assets.dart` → ``CareerAssets``.

    ⚠️ Counts (workCount / projectCount / skillCount …) are **derived by the
    app** — the backend must never send count fields.
    """

    workExperiences: list[WorkExperience] = Field(default_factory=list)
    projects: list[ProjectExperience] = Field(default_factory=list)
    skillGroups: list[SkillGroup] = Field(default_factory=list)
    awards: list[Award] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    spanLabel: str = ""
    monthlyAdded: int = 0


# ══════════════════════════════════════════════════════════════════════════
#  Profile / home
# ══════════════════════════════════════════════════════════════════════════


class UserProfile(BaseModel):
    """`user_profile.dart` → ``UserProfile``."""

    displayName: str = ""
    handle: str = ""
    membershipLabel: str = ""


class MatchChange(BaseModel):
    """`home_summary.dart` → ``MatchChange``."""

    jobId: str
    jobTitle: str = ""
    company: Optional[str] = None
    score: int = 0
    delta: Optional[int] = None


class HomeSummary(BaseModel):
    """`home_summary.dart` → ``HomeSummary``."""

    headline: str = ""
    subheadline: str = ""
    suggestions: list[str] = Field(default_factory=list)
    matchChanges: list[MatchChange] = Field(default_factory=list)
    recentGrowth: list[str] = Field(default_factory=list)
    dailyContent: Optional[str] = None
    hasBaseline: bool = False


# ── helpers ───────────────────────────────────────────────────────────────


def iso(ts: float | None) -> Optional[str]:
    """Unix seconds -> ISO-8601 string that ``DateTime.tryParse`` accepts."""
    if ts is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def dump(model: BaseModel) -> dict[str, Any]:
    """``model_dump`` with ``None`` preserved (Dart relies on null checks)."""
    return model.model_dump(mode="json")
