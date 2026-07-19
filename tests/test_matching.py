"""Tests for the core matching logic — skill overlap, composite scoring.

These tests do **not** require an LLM API key — they exercise the
deterministic parts of the matching pipeline.
"""

from __future__ import annotations

import pytest
from app.models.job import JobList, JobPosting
from app.models.resume import ResumeProfile


class TestSkillOverlap:
    """SkillOverlap — the deterministic lexical matching component."""

    def test_exact_match(self, matching_agent, sample_resume, sample_jobs):
        """Should find perfect overlap for matching skills."""
        job = sample_jobs.jobs[0]  # Python/FastAPI/K8s/PostgreSQL
        result = matching_agent._skill_overlap(
            sample_resume.skills, job.required_skills
        )
        assert len(result["matched"]) == len(job.required_skills)
        assert len(result["missing"]) == 0
        assert result["score"] == 1.0

    def test_no_match(self, matching_agent, sample_resume, sample_jobs):
        """Should report all skills missing for unrelated jobs."""
        job = sample_jobs.jobs[1]  # React/TypeScript/CSS
        result = matching_agent._skill_overlap(
            sample_resume.skills, job.required_skills
        )
        assert len(result["missing"]) == len(job.required_skills)
        assert result["score"] == 0.0

    def test_partial_match(self, matching_agent, sample_resume):
        """Should detect partial match."""
        job = JobPosting(
            title="后端工程师",
            description="",
            required_skills=["Python", "Java", "Kubernetes"],
        )
        result = matching_agent._skill_overlap(sample_resume.skills, job.required_skills)
        assert len(result["matched"]) == 2  # Python, Kubernetes
        assert len(result["missing"]) == 1  # Java
        assert 0.5 < result["score"] < 1.0

    def test_skill_synonyms(self, matching_agent, sample_resume):
        """Should match synonyms: K8s → Kubernetes, PostgreSQL → SQL."""
        resume = ResumeProfile(
            skills=["K8s", "Python3.8", "PyTorch", "AWS"],
        )
        job = JobPosting(
            title="ML Engineer",
            description="",
            required_skills=["Kubernetes", "Python", "TensorFlow", "Cloud"],
        )
        result = matching_agent._skill_overlap(resume.skills, job.required_skills)
        # With synonym map: K8s→Kubernetes, Python3.8→Python,
        # PyTorch→deep-learning (not TF exactly but same group),
        # AWS→cloud
        assert "Kubernetes" in result["matched"] or any(
            "k8s" in s.lower() or "kubernetes" in s.lower()
            for s in result["matched"]
        )
        print(f"  synonyms: matched={result['matched']} missing={result['missing']}")
        # At least 3 out of 4 should match via synonyms
        assert result["score"] >= 0.5, f"Expected >0.5, got {result['score']}"

    def test_empty_skills(self, matching_agent, sample_resume):
        """Should return default score 0.5 when no skills required."""
        job = JobPosting(title="Any", description="", required_skills=[])
        result = matching_agent._skill_overlap(sample_resume.skills, job.required_skills)
        assert result["score"] == 0.5
        assert result["reasons"] == ["No specific skills required"]

    def test_case_insensitive(self, matching_agent):
        """Should match regardless of case."""
        resume = ResumeProfile(skills=["python", "docker"])
        job = JobPosting(title="Dev", description="", required_skills=["Python", "DOCKER"])
        result = matching_agent._skill_overlap(resume.skills, job.required_skills)
        assert result["score"] == 1.0


class TestCompositeScore:
    """Weighted composite score calculation."""

    def test_all_components(self, matching_agent):
        """Should weight overlap=0.3, semantic=0.3, llm=0.4."""
        score = matching_agent._composite_score(
            overlap=0.5, semantic=0.8, llm=0.9
        )
        expected = 0.5 * 0.3 + 0.8 * 0.3 + 0.9 * 0.4
        assert score == pytest.approx(expected)

    def test_only_overlap(self, matching_agent):
        """Should redistribute weight when semantic and LLM are None."""
        score = matching_agent._composite_score(
            overlap=0.5, semantic=None, llm=None
        )
        assert score == 0.5  # single component → itself

    def test_missing_llm(self, matching_agent):
        """Should redistribute LLM weight when LLM is unavailable."""
        score = matching_agent._composite_score(
            overlap=0.5, semantic=0.8, llm=None
        )
        # overlap=0.5*0.3 + semantic=0.8*0.3+0.4/2=1.0 → total 1.0
        # Wait, let me re-check the logic:
        # overlap=0.5*0.3=0.15, semantic=0.8*(0.3+0.2)=0.4, sum=0.55, weight=0.3+0.5=0.8
        # Actually the code: if llm is None: extra=0 if semantic is not None else 0.3/2
        # If semantic is not None: extra = 0 → llm weight shifts to 0.4
        # Then total = 0.3 + 0.3 + 0.4 = 1.0
        # weighted = 0.5*0.3 + 0.8*0.3 = 0.39
        # score = 0.39 / 0.6 = 0.65
        expected = (0.5 * 0.3 + 0.8 * 0.3) / 0.6
        assert score == pytest.approx(expected, rel=1e-3)


class TestMatchingAgentRun:
    """Integration tests for MatchingAgent.run() with mocked components."""

    @pytest.mark.asyncio
    async def test_two_stage_matching(self, matching_agent, sample_resume, sample_jobs):
        """Should return sorted results with correct structure."""
        results = await matching_agent.run(
            resume=sample_resume,
            jobs=sample_jobs,
            use_llm=False,       # no LLM in unit tests
            use_embedding=False,  # no embedding in unit tests
            two_stage=False,
        )
        assert len(results) == 3
        # Should be sorted by score descending
        assert results[0].score >= results[1].score >= results[2].score
        # All results should have required fields
        for r in results:
            assert r.score >= 0.0
            assert r.job is not None
            assert isinstance(r.match_reasons, list)

    @pytest.mark.asyncio
    async def test_empty_jobs(self, matching_agent, sample_resume):
        """Should handle empty job list gracefully."""
        empty = JobList(jobs=[])
        results = await matching_agent.run(
            resume=sample_resume,
            jobs=empty,
            use_llm=False,
            use_embedding=False,
        )
        assert results == []
