"""Match result data models — core scoring output."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .job import JobPosting
from .resume import ResumeProfile


class SkillGap(BaseModel):
    """A single skill gap identified during matching."""

    skill: str = Field(..., description="Missing or weak skill")
    severity: str = Field(default="medium", description="gap | weak | missing")
    suggestion: Optional[str] = Field(None, description="How to address this gap")


class MatchResult(BaseModel):
    """Result of matching one resume against one job posting."""

    job: JobPosting = Field(..., description="The job posting evaluated")
    score: float = Field(..., ge=0.0, le=1.0, description="Overall match score 0–1")

    # Detailed breakdown
    skill_overlap: list[str] = Field(default_factory=list, description="Skills that match")
    missing_skills: list[str] = Field(default_factory=list, description="Required skills candidate lacks")
    partial_skills: list[str] = Field(default_factory=list, description="Skills partially met")

    # Reasoning
    match_reasons: list[str] = Field(default_factory=list, description="Why this is a good match")
    gaps: list[SkillGap] = Field(default_factory=list, description="Identified gaps")

    # Semantic
    semantic_score: Optional[float] = Field(None, ge=0.0, le=1.0, description="Embedding-based similarity")
    llm_score: Optional[float] = Field(None, ge=0.0, le=1.0, description="LLM-assessed score")

    # Recommendations
    recommendations: list[str] = Field(default_factory=list, description="Actionable improvement suggestions")

    model_config = {"frozen": False}

    def grade(self) -> str:
        """Letter grade based on score."""
        if self.score >= 0.85:
            return "A"
        elif self.score >= 0.70:
            return "B"
        elif self.score >= 0.50:
            return "C"
        else:
            return "D"

    def summary(self, verbose: bool = False) -> str:
        """One-line or multi-line summary."""
        base = (
            f"[{self.grade()}] {self.job.title} @ {self.job.company or '?'} "
            f"— Score: {self.score:.2f} | Overlap: {len(self.skill_overlap)} "
            f"| Missing: {len(self.missing_skills)}"
        )
        if verbose:
            gaps = "\n    ".join(f"❌ {g.skill} ({g.severity}): {g.suggestion or '—'}" for g in self.gaps[:5])
            recs = "\n    ".join(f"💡 {r}" for r in self.recommendations[:3])
            return f"{base}\n  Gaps:\n    {gaps}\n  Recommendations:\n    {recs}"
        return base


class MatchReport(BaseModel):
    """Complete report for one resume × N jobs."""

    resume: ResumeProfile = Field(..., description="The candidate profile used")
    results: list[MatchResult] = Field(default_factory=list, description="One result per job")
    total_jobs: int = 0
    average_score: Optional[float] = Field(None, description="Average match score")

    model_config = {"frozen": False}

    def top_n(self, n: int = 5) -> list[MatchResult]:
        """Return the top-N results by score."""
        sorted_results = sorted(self.results, key=lambda r: r.score, reverse=True)
        return sorted_results[:n]

    def summary(self) -> str:
        """High-level report summary."""
        ranked = sorted(self.results, key=lambda r: r.score, reverse=True)
        top5_lines = []
        for i, r in enumerate(ranked[:5], 1):
            top5_lines.append(f"  {i}. [{r.grade()}] {r.job.title} — {r.score:.2f}")
        return (
            f"📊 Match Report for {self.resume.name or 'Candidate'}\n"
            f"  Jobs evaluated: {self.total_jobs}\n"
            f"  Average score:  {self.average_score:.3f}\n"
            f"  Top matches:\n" + "\n".join(top5_lines)
        )
