"""Matching Agent — 2-stage ranking engine (Upgraded).

Stage 1 (Fast):  Embedding similarity filter → top 20% candidates
Stage 2 (Deep):  LLM rerank + skill overlap → final scores + gap analysis

Cost reduction: ~80% fewer LLM calls vs brute-force matching.
Scalability:   Handles 1000+ jobs via embedding pre-filter.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

from app.models.job import JobList, JobPosting
from app.models.match import MatchResult, SkillGap
from app.models.resume import ResumeProfile
from app.services.embedding_service import EmbeddingService
from app.services.llm_service import LLMService
from app.utils.logger import logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MATCH_OUTPUT_DIR = DATA_DIR / "outputs"


class MatchingAgent:
    """Core matching engine — 2-stage: embedding filter → LLM rerank."""

    def __init__(
        self,
        llm: Optional[LLMService] = None,
        embedder: Optional[EmbeddingService] = None,
        semantic_weight: float = 0.3,
        llm_weight: float = 0.4,
        overlap_weight: float = 0.3,
        # 2-stage defaults
        stage1_top_k: int = 10,
        stage1_top_ratio: float = 0.2,
        stage1_min_score: float = 0.3,
    ):
        self.llm = llm or LLMService()
        self.embedder = embedder or EmbeddingService()
        self.semantic_weight = semantic_weight
        self.llm_weight = llm_weight
        self.overlap_weight = overlap_weight
        self.stage1_top_k = stage1_top_k
        self.stage1_top_ratio = stage1_top_ratio
        self.stage1_min_score = stage1_min_score
        self.session_id: str = ""
        MATCH_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Public interface ──────────────────────────────────────────────────

    async def run(
        self,
        resume: ResumeProfile,
        jobs: JobList,
        use_llm: bool = True,
        use_embedding: bool = True,
        two_stage: bool = True,
        session_id: Optional[str] = None,
        # Optional pre-computed embeddings (from cache)
        resume_embedding: Optional[list[float]] = None,
        job_embeddings: Optional[list[list[float]]] = None,
    ) -> list[MatchResult]:
        """Match resume against jobs. Supports single-pass or 2-stage.

        Args:
            resume: Candidate's parsed profile.
            jobs: Collection of job postings.
            use_llm: Enable LLM-based evaluation (Stage 2).
            use_embedding: Enable embedding scoring (Stage 1).
            two_stage: Use 2-stage pipeline. If False, runs full LLM on all jobs.
            session_id: Traceability ID.
            resume_embedding: Pre-computed resume embedding (from cache).
            job_embeddings: Pre-computed job embeddings (from cache).

        Returns:
            Sorted list of MatchResults (highest score first).
        """
        self.session_id = session_id or f"match-{uuid.uuid4().hex[:8]}"
        logger.info(
            "MatchingAgent starting | resume=%s | jobs=%d | two_stage=%s",
            resume.name, jobs.total_count, two_stage,
        )

        if not jobs.jobs:
            logger.warning("No jobs to match")
            return []

        if two_stage and use_embedding and len(jobs.jobs) > self.stage1_top_k:
            return await self._run_two_stage(
                resume, jobs, use_llm,
                resume_embedding, job_embeddings,
            )
        else:
            return await self._run_full_pass(resume, jobs, use_llm, use_embedding)

    # ── 2-Stage pipeline ─────────────────────────────────────────────────

    async def _run_two_stage(
        self,
        resume: ResumeProfile,
        jobs: JobList,
        use_llm: bool,
        resume_embedding: Optional[list[float]],
        job_embeddings: Optional[list[list[float]]],
    ) -> list[MatchResult]:
        """2-Stage: embedding filter → LLM/overlap rerank on top candidates only."""
        resume_text = self._resume_for_prompt(resume)

        # ═══ Stage 1: Embedding filter ═══════════════════════════════════
        logger.info("Stage 1: Embedding filter on %d jobs", len(jobs.jobs))

        if resume_embedding is None:
            resume_emb = self.embedder.embed(resume_text)
        else:
            resume_emb = resume_embedding

        if job_embeddings is None:
            job_texts = [j.description[:3000] or j.title for j in jobs.jobs]
            job_embs = self.embedder.embed_many(job_texts)
        else:
            job_embs = job_embeddings

        top_candidates = self.embedder.filter_top_k(
            target_embedding=resume_emb,
            candidate_embeddings=job_embs,
            k=self.stage1_top_k,
            ratio=self.stage1_top_ratio,
            min_score=self.stage1_min_score,
        )

        passed = len(top_candidates)
        logger.info(
            "Stage 1 filter: %d/%d passed (k=%d, ratio=%.2f, min=%.2f)",
            passed, len(jobs.jobs), self.stage1_top_k, self.stage1_top_ratio, self.stage1_min_score,
        )

        if not top_candidates:
            logger.warning("Stage 1 filter returned zero candidates — using top %d by index", self.stage1_top_k)
            top_candidates = [(i, 0.0) for i in range(min(self.stage1_top_k, len(jobs.jobs)))]

        # ═══ Stage 2: LLM rerank + overlap ═══════════════════════════════
        logger.info("Stage 2: LLM rerank on %d candidates", passed)
        results: list[MatchResult] = []

        for job_idx, sem_score in top_candidates:
            job = jobs.jobs[job_idx]
            try:
                result = self._match_one(
                    resume, resume_text, job,
                    use_llm=use_llm,
                    use_embedding=False,  # semantic already done in stage 1
                    override_semantic=sem_score,
                )
                results.append(result)
                logger.debug("Stage 2 | %s → %.3f (sem=%.3f)", job.title, result.score, sem_score)
            except Exception as e:
                logger.error("Stage 2 failed for job '%s': %s", job.title, e)
                results.append(self._fallback_result(job))
                continue

        # Re-sort by final composite score
        results.sort(key=lambda r: r.score, reverse=True)
        self._save_checkpoint(results)
        logger.info(
            "MatchingAgent 2-stage done | passed=%d | top=%.3f",
            passed, results[0].score if results else 0,
        )
        return results

    # ── Single-pass fallback (direct LLM on all jobs) ────────────────────

    async def _run_full_pass(
        self,
        resume: ResumeProfile,
        jobs: JobList,
        use_llm: bool,
        use_embedding: bool,
    ) -> list[MatchResult]:
        """Legacy single-pass: evaluate all jobs with all methods."""
        logger.info("Full-pass matching on %d jobs", len(jobs.jobs))
        results: list[MatchResult] = []
        resume_text = self._resume_for_prompt(resume)

        for job in jobs.jobs:
            try:
                result = self._match_one(resume, resume_text, job, use_llm, use_embedding)
                results.append(result)
            except Exception as e:
                logger.error("Match failed for '%s': %s", job.title, e)
                results.append(self._fallback_result(job))
                continue

        results.sort(key=lambda r: r.score, reverse=True)
        self._save_checkpoint(results)
        logger.info("Full-pass done | total=%d | top=%.3f", len(results), results[0].score if results else 0)
        return results

    # ── Embedding computation (for cache / orchestrator) ─────────────────

    def compute_job_embeddings(self, jobs: JobList) -> list[list[float]]:
        """Batch-compute embeddings for all jobs (for cache)."""
        texts = [
            f"{j.title}\n{j.description[:2000]}" if j.description else j.title
            for j in jobs.jobs
        ]
        return self.embedder.embed_many(texts)

    def compute_resume_embedding(self, resume: ResumeProfile) -> list[float]:
        """Compute resume embedding (for cache)."""
        text = self._resume_for_prompt(resume)
        return self.embedder.embed(text)

    # ── Single job matching ───────────────────────────────────────────────

    def _match_one(
        self,
        resume: ResumeProfile,
        resume_text: str,
        job: JobPosting,
        use_llm: bool,
        use_embedding: bool,
        override_semantic: Optional[float] = None,
    ) -> MatchResult:
        """Evaluate one resume × job pair.

        Args:
            override_semantic: If provided, skip embedding call and use this value.
        """
        # 1. Skill overlap scoring
        overlap = self._skill_overlap(resume.skills, job.required_skills)
        overlap_score = overlap["score"]

        # 2. Semantic embedding similarity (skip if override provided)
        sem_score = override_semantic
        if sem_score is None and use_embedding:
            try:
                sem_score = self.embedder.similarity(resume_text, job.description[:3000])
            except Exception as e:
                logger.warning("Embedding failed: %s", e)

        # 3. LLM reasoning
        llm_result: dict = {}
        llm_score = None
        if use_llm:
            try:
                llm_result = self.llm.match(
                    resume_text, job.description[:4000], job.title
                )
                llm_score = float(llm_result.get("score", 0.5))
            except Exception as e:
                logger.warning("LLM match failed: %s", e)

        # 4. Composite score
        composite = self._composite_score(overlap_score, sem_score, llm_score)

        # 5. Build result
        gaps_raw = llm_result.get("gaps", [])
        return MatchResult(
            job=job,
            score=round(composite, 4),
            skill_overlap=overlap["matched"],
            missing_skills=overlap["missing"],
            partial_skills=llm_result.get("partial_skills", []),
            match_reasons=llm_result.get("match_reasons", overlap["reasons"]),
            gaps=[
                SkillGap(
                    skill=g.get("skill", g) if isinstance(g, str) else g.get("skill", "Unknown"),
                    severity=g.get("severity", "missing") if isinstance(g, dict) else "missing",
                    suggestion=g.get("suggestion") if isinstance(g, dict) else None,
                )
                for g in gaps_raw
            ],
            semantic_score=round(sem_score, 4) if sem_score is not None else None,
            llm_score=round(llm_score, 4) if llm_score is not None else None,
            recommendations=llm_result.get("recommendations", []),
        )

    # ── Scoring components ────────────────────────────────────────────────

    @staticmethod
    def _skill_overlap(
        candidate_skills: list[str],
        required_skills: list[str],
    ) -> dict:
        """Compute lexical skill overlap between candidate and job."""
        c_skills = {s.strip().lower() for s in candidate_skills if s.strip()}
        r_skills = {s.strip().lower() for s in required_skills if s.strip()}

        if not r_skills:
            return {"matched": [], "missing": [], "score": 0.5, "reasons": ["No specific skills required"]}

        matched = [s for s in required_skills if s.strip().lower() in c_skills]
        missing = [s for s in required_skills if s.strip().lower() not in c_skills]

        score = len(matched) / len(r_skills) if r_skills else 0.5

        # Partial match boost
        for cs in c_skills:
            for rs in list(r_skills - {s.strip().lower() for s in matched}):
                if cs in rs or rs in cs:
                    score += 0.05 * (1 - score)
                    break

        score = min(score, 1.0)

        reasons = []
        if matched:
            reasons.append(f"✅ Skills matched: {', '.join(matched[:5])}")
        if missing:
            reasons.append(f"❌ Missing: {', '.join(missing[:5])}")

        return {"matched": matched, "missing": missing, "score": score, "reasons": reasons}

    def _composite_score(
        self,
        overlap: float,
        semantic: Optional[float],
        llm: Optional[float],
    ) -> float:
        """Weighted combination of all scoring dimensions."""
        components = []
        components.append((overlap, self.overlap_weight))

        if semantic is not None:
            components.append((semantic, self.semantic_weight))

        if llm is not None:
            extra = 0 if semantic is not None else self.semantic_weight / 2
            components.append((llm, self.llm_weight + extra))

        total_weight = sum(w for _, w in components)
        if total_weight == 0:
            return 0.5

        weighted = sum(s * w for s, w in components)
        return weighted / total_weight

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _resume_for_prompt(resume: ResumeProfile) -> str:
        """Format resume into a concise text block."""
        parts = []
        if resume.name:
            parts.append(f"姓名: {resume.name}")
        if resume.roles:
            parts.append(f"角色: {', '.join(resume.roles)}")
        if resume.experience_years is not None:
            parts.append(f"经验: {resume.experience_years}年")
        if resume.skills:
            parts.append(f"技能: {', '.join(resume.skills)}")
        if resume.projects:
            for p in resume.projects:
                tech = ", ".join(p.tech_stack) if p.tech_stack else ""
                parts.append(f"项目: {p.name} | {tech} | {p.description or ''}")
        if resume.education:
            for e in resume.education:
                parts.append(f"教育: {e.degree} - {e.institution}")
        if resume.certifications:
            parts.append(f"证书: {', '.join(resume.certifications)}")
        if resume.raw_text:
            parts.append(f"\n原始简历:\n{resume.raw_text[:2000]}")
        return "\n".join(parts)

    @staticmethod
    def _fallback_result(job: JobPosting) -> MatchResult:
        return MatchResult(
            job=job, score=0.0,
            match_reasons=["⚠️ Matching encountered an error"],
            recommendations=["Please retry matching"],
            gaps=[SkillGap(skill="N/A", severity="missing", suggestion="Retry required")],
        )

    def _save_checkpoint(self, results: list[MatchResult]) -> None:
        path = MATCH_OUTPUT_DIR / f"match_result_{self.session_id}.json"
        path.write_text(
            json.dumps(
                [{"job_title": r.job.title, "company": r.job.company, "score": r.score,
                  "grade": r.grade(), "skill_overlap": r.skill_overlap,
                  "missing_skills": r.missing_skills, "match_reasons": r.match_reasons,
                  "gaps": [g.model_dump() for g in r.gaps],
                  "recommendations": r.recommendations} for r in results],
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("Checkpoint saved → %s", path)
