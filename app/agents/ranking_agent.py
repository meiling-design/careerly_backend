"""Ranking & Report Agent — sort, format, and export final results."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

from app.models.job import JobList
from app.models.match import MatchReport, MatchResult
from app.models.resume import ResumeProfile
from app.services.export_service import ExportService
from app.utils.logger import logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OUTPUT_DIR = DATA_DIR / "outputs"


class RankingAgent:
    """Sort results, generate reports, export to Excel/CSV/Markdown."""

    def __init__(self, export: Optional[ExportService] = None):
        self.export = export or ExportService()
        self.session_id: str = ""
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Main entry point ──────────────────────────────────────────────────

    async def run(
        self,
        results: list[MatchResult],
        resume: ResumeProfile,
        jobs: JobList,
        session_id: Optional[str] = None,
    ) -> MatchReport:
        """Build a complete MatchReport from raw results.

        Args:
            results: List of individual match results (unsorted).
            resume: The candidate profile used.
            jobs: Original job list (for reference).
            session_id: Optional traceability ID.

        Returns:
            MatchReport with sorted results, scores, and averages.
        """
        self.session_id = session_id or f"rank-{uuid.uuid4().hex[:8]}"
        logger.info("RankingAgent starting | results=%d", len(results))

        # Sort by score descending
        sorted_results = sorted(results, key=lambda r: r.score, reverse=True)

        avg_score = (
            round(sum(r.score for r in sorted_results) / len(sorted_results), 4)
            if sorted_results
            else 0.0
        )

        report = MatchReport(
            resume=resume,
            results=sorted_results,
            total_jobs=len(sorted_results),
            average_score=avg_score,
        )

        # Export
        self._export_formats(report)
        logger.info("RankingAgent done | top=%s (%.3f) | avg=%.3f",
                      sorted_results[0].job.title if sorted_results else "N/A",
                      sorted_results[0].score if sorted_results else 0,
                      avg_score)
        return report

    # ── Export ────────────────────────────────────────────────────────────

    def _export_formats(self, report: MatchReport) -> None:
        """Save report in all supported formats."""
        prefix = OUTPUT_DIR / f"report_{self.session_id}"

        try:
            self.export.save_markdown(report, prefix.with_suffix(".md"))
        except Exception as e:
            logger.warning("Markdown export failed: %s", e)

        try:
            self.export.save_csv(report, prefix.with_suffix(".csv"))
        except Exception as e:
            logger.warning("CSV export failed: %s", e)

        try:
            self.export.save_excel(report, prefix.with_suffix(".xlsx"))
        except Exception as e:
            logger.warning("Excel export failed: %s", e)

        # JSON raw data
        json_path = prefix.with_suffix(".json")
        try:
            json_path.write_text(
                json.dumps(
                    {
                        "session_id": self.session_id,
                        "resume_name": report.resume.name,
                        "average_score": report.average_score,
                        "total_jobs": report.total_jobs,
                        "results": [
                            {
                                "job_title": r.job.title,
                                "company": r.job.company,
                                "location": r.job.location,
                                "score": r.score,
                                "grade": r.grade(),
                                "match_reasons": r.match_reasons,
                                "missing_skills": r.missing_skills,
                                "skill_overlap": r.skill_overlap,
                                "gaps": [g.model_dump() for g in r.gaps],
                                "recommendations": r.recommendations,
                            }
                            for r in report.results
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            logger.info("JSON report saved → %s", json_path)
        except Exception as e:
            logger.warning("JSON export failed: %s", e)

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def get_table_data(report: MatchReport) -> list[dict]:
        """Return report data as a list of dicts (for UI tables)."""
        return [
            {
                "rank": i + 1,
                "grade": r.grade(),
                "title": r.job.title,
                "company": r.job.company or "",
                "location": r.job.location or "",
                "score": f"{r.score:.1%}",
                "overlap": len(r.skill_overlap),
                "gaps": len(r.missing_skills),
                "recommendations": r.recommendations[:2],
            }
            for i, r in enumerate(report.results)
        ]

    @staticmethod
    def top_recommendations(report: MatchReport, n: int = 3) -> list[str]:
        """Aggregate top recommendations across all matches."""
        all_recs: list[str] = []
        for r in report.results[:5]:
            all_recs.extend(r.recommendations)
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique = []
        for rec in all_recs:
            if rec not in seen:
                seen.add(rec)
                unique.append(rec)
        return unique[:n]
