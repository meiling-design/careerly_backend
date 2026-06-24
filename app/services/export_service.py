"""Export service — generates Excel, CSV, and Markdown reports."""

from __future__ import annotations

import csv
import io
from pathlib import Path
from typing import Optional

from app.models.match import MatchReport, MatchResult
from app.utils.logger import logger


class ExportService:
    """Export match results to various formats."""

    # ── Markdown ──────────────────────────────────────────────────────────

    @staticmethod
    def to_markdown(report: MatchReport) -> str:
        """Render full match report as Markdown."""
        lines = [
            f"# 📊 Job Match Report",
            f"**Candidate:** {report.resume.name or 'Unknown'}",
            f"**Jobs Evaluated:** {report.total_jobs}",
            f"**Average Score:** {report.average_score:.3f}" if report.average_score else "",
            "",
            "---",
            "",
        ]

        sorted_results = sorted(report.results, key=lambda r: r.score, reverse=True)
        for i, r in enumerate(sorted_results, 1):
            lines.extend([
                f"## {i}. [{r.grade()}] {r.job.title} @ {r.job.company or 'Unknown'}",
                f"**Score:** {r.score:.2f} | **Location:** {r.job.location or 'Remote'}",
                "",
                "**Match Reasons:**",
            ] + [f"- {reason}" for reason in r.match_reasons] + [
                "",
                "**Skill Overlap:** " + ", ".join(r.skill_overlap[:8]) + (" ..." if len(r.skill_overlap) > 8 else ""),
                "",
            ])

            if r.gaps:
                lines.append("**Gaps:**")
                for g in r.gaps[:5]:
                    lines.append(f"- ❌ {g.skill} ({g.severity}): {g.suggestion or '—'}")
                lines.append("")

            if r.recommendations:
                lines.append("**Recommendations:**")
                for rec in r.recommendations[:3]:
                    lines.append(f"- 💡 {rec}")
                lines.append("")

            lines.append("---")

        return "\n".join(lines)

    # ── CSV ───────────────────────────────────────────────────────────────

    @staticmethod
    def to_csv(report: MatchReport) -> str:
        """Render results as CSV string."""
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Rank", "Grade", "Job Title", "Company", "Location",
            "Score", "Match Reasons", "Skill Overlap", "Missing Skills",
            "Recommendations",
        ])

        sorted_results = sorted(report.results, key=lambda r: r.score, reverse=True)
        for i, r in enumerate(sorted_results, 1):
            writer.writerow([
                i,
                r.grade(),
                r.job.title,
                r.job.company or "",
                r.job.location or "",
                f"{r.score:.2f}",
                "; ".join(r.match_reasons),
                "; ".join(r.skill_overlap),
                "; ".join(r.missing_skills),
                "; ".join(r.recommendations),
            ])

        return output.getvalue()

    # ── Excel ─────────────────────────────────────────────────────────────

    @staticmethod
    def to_excel(report: MatchReport) -> bytes:
        """Generate Excel workbook in-memory as bytes."""
        try:
            import openpyxl  # type: ignore[import-untyped]
            from openpyxl.styles import Font, PatternFill
        except ImportError:
            logger.warning("openpyxl not installed, falling back to CSV bytes")
            return ExportService.to_csv(report).encode("utf-8")

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Match Results"

        # Header row
        headers = ["Rank", "Grade", "Job Title", "Company", "Location",
                    "Score", "Match Reasons", "Skill Overlap", "Missing Skills", "Recommendations"]
        header_font = Font(bold=True)
        header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
        header_font_white = Font(bold=True, color="FFFFFF")

        for col, h in enumerate(headers, 1):
            cell = ws.cell(row=1, column=col, value=h)
            cell.font = header_font_white
            cell.fill = header_fill

        # Data rows
        sorted_results = sorted(report.results, key=lambda r: r.score, reverse=True)
        for row_idx, r in enumerate(sorted_results, 2):
            grade = r.grade()
            # Color per grade
            grade_colors = {
                "A": "C6EFCE", "B": "D9E2F3", "C": "FFF2CC", "D": "FCE4EC"
            }
            fill = PatternFill(
                start_color=grade_colors.get(grade, "FFFFFF"),
                end_color=grade_colors.get(grade, "FFFFFF"),
                fill_type="solid",
            )

            data = [
                row_idx - 1,
                grade,
                r.job.title,
                r.job.company or "",
                r.job.location or "",
                round(r.score, 2),
                "; ".join(r.match_reasons),
                "; ".join(r.skill_overlap),
                "; ".join(r.missing_skills),
                "; ".join(r.recommendations),
            ]
            for col, val in enumerate(data, 1):
                cell = ws.cell(row=row_idx, column=col, value=val)
                cell.fill = fill

        # Auto-width
        for col in ws.columns:
            max_len = 0
            col_letter = col[0].column_letter
            for cell in col:
                if cell.value:
                    max_len = max(max_len, len(str(cell.value)))
            ws.column_dimensions[col_letter].width = min(max_len + 2, 60)

        output = io.BytesIO()
        wb.save(output)
        output.seek(0)
        return output.getvalue()

    # ── File save helpers ─────────────────────────────────────────────────

    @staticmethod
    def save_csv(report: MatchReport, path: str | Path) -> Path:
        """Save CSV to disk."""
        path = Path(path)
        path.write_text(ExportService.to_csv(report), encoding="utf-8")
        logger.info("CSV report saved → %s", path)
        return path

    @staticmethod
    def save_excel(report: MatchReport, path: str | Path) -> Path:
        """Save Excel to disk."""
        path = Path(path)
        path.write_bytes(ExportService.to_excel(report))
        logger.info("Excel report saved → %s", path)
        return path

    @staticmethod
    def save_markdown(report: MatchReport, path: str | Path) -> Path:
        """Save Markdown report to disk."""
        path = Path(path)
        path.write_text(ExportService.to_markdown(report), encoding="utf-8")
        logger.info("Markdown report saved → %s", path)
        return path
