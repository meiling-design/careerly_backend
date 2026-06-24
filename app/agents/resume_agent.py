"""Resume Agent — parse PDF / DOCX / TXT resumes into structured profiles.

Supports:
- PDF via pdfplumber
- DOCX via python-docx
- Plain text
- LLM-enhanced structured extraction
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Optional

from app.models.resume import Education, Project, ResumeProfile
from app.services.llm_service import LLMService
from app.utils.logger import logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RESUME_OUTPUT_DIR = DATA_DIR / "outputs"


class ResumeAgent:
    """Parse and structure resume documents."""

    def __init__(self, llm: Optional[LLMService] = None):
        self.llm = llm or LLMService()
        self.session_id: str = ""
        RESUME_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ── Main entry point ──────────────────────────────────────────────────

    async def run(
        self,
        file_path: Optional[str] = None,
        text: Optional[str] = None,
        use_llm: bool = True,
        session_id: Optional[str] = None,
    ) -> ResumeProfile:
        """Parse a resume from file path or raw text.

        Args:
            file_path: Path to PDF/DOCX/TXT resume file.
            text: Raw resume text (alternative to file_path).
            use_llm: Whether to use LLM for structured extraction.
            session_id: Optional session ID for traceability.

        Returns:
            ResumeProfile with extracted fields.
        """
        self.session_id = session_id or f"resume-{uuid.uuid4().hex[:8]}"

        # Extract raw text
        raw_text = text or ""
        if file_path:
            raw_text = self._extract_text(file_path)
            logger.info("ResumeAgent loaded file | path=%s | chars=%d", file_path, len(raw_text))

        if not raw_text or len(raw_text.strip()) < 20:
            logger.warning("Resume text too short (%d chars), returning empty profile", len(raw_text or ""))
            return ResumeProfile(raw_text=raw_text or "")

        if use_llm:
            profile = self._parse_with_llm(raw_text)
        else:
            profile = ResumeProfile(raw_text=raw_text)

        profile.raw_text = raw_text[:5000]  # keep raw text for reference
        self._save_checkpoint(profile)
        logger.info("ResumeAgent done | name=%s | skills=%d | exp=%s",
                      profile.name, len(profile.skills), profile.experience_years)
        return profile

    # ── Text Extraction ───────────────────────────────────────────────────

    def _extract_text(self, file_path: str) -> str:
        """Extract text from PDF, DOCX, or TXT file."""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Resume file not found: {file_path}")

        suffix = path.suffix.lower()

        if suffix == ".pdf":
            return self._extract_pdf(path)
        elif suffix == ".docx":
            return self._extract_docx(path)
        elif suffix == ".txt":
            return path.read_text(encoding="utf-8", errors="replace")
        else:
            raise ValueError(f"Unsupported file type: {suffix}. Supported: .pdf, .docx, .txt")

    def _extract_pdf(self, path: Path) -> str:
        """Extract text from PDF using pdfplumber."""
        try:
            import pdfplumber  # type: ignore[import-untyped]
            text_parts: list[str] = []
            with pdfplumber.open(path) as pdf:
                for page in pdf.pages:
                    page_text = page.extract_text()
                    if page_text:
                        text_parts.append(page_text)
            result = "\n".join(text_parts)
            if not result.strip():
                logger.warning("pdfplumber returned empty, trying OCR fallback")
                return self._ocr_extract(path)
            return result
        except ImportError:
            logger.warning("pdfplumber not installed, trying OCR fallback")
            return self._ocr_extract(path)

    def _extract_docx(self, path: Path) -> str:
        """Extract text from DOCX using python-docx."""
        try:
            from docx import Document  # type: ignore[import-untyped]
            doc = Document(str(path))
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except ImportError:
            logger.warning("python-docx not installed")
            raise ImportError("Install python-docx for .docx support: pip install python-docx")

    def _ocr_extract(self, path: Path) -> str:
        """Fallback OCR extraction using pytesseract (last resort)."""
        try:
            import pdf2image  # type: ignore[import-untyped]
            import pytesseract  # type: ignore[import-untyped]
            images = pdf2image.convert_from_path(str(path))
            text = "\n".join(pytesseract.image_to_string(img, lang="chi_sim+eng") for img in images)
            return text
        except ImportError:
            logger.warning("OCR dependencies not available (pdf2image/pytesseract)")
            return ""

    # ── LLM Parsing ───────────────────────────────────────────────────────

    def _parse_with_llm(self, raw_text: str) -> ResumeProfile:
        """Use LLM to extract structured fields from resume text."""
        try:
            result = self.llm.parse_resume(raw_text[:8000])
            if not result:
                logger.warning("LLM returned empty result for resume")
                return ResumeProfile(raw_text=raw_text)

            projects = [
                Project(
                    name=p.get("name", "Unknown"),
                    description=p.get("description"),
                    tech_stack=p.get("tech_stack", []),
                    highlights=p.get("highlights", []),
                )
                for p in (result.get("projects") or [])
                if p.get("name")
            ]

            education = [
                Education(
                    degree=e.get("degree", ""),
                    institution=e.get("institution", ""),
                    year=e.get("year"),
                )
                for e in (result.get("education") or [])
                if e.get("degree")
            ]

            return ResumeProfile(
                name=result.get("name"),
                email=result.get("email"),
                phone=result.get("phone"),
                skills=result.get("skills", []),
                experience_years=result.get("experience_years"),
                roles=result.get("roles", []),
                projects=projects,
                education=education,
                certifications=result.get("certifications", []),
            )
        except Exception as e:
            logger.error("LLM resume parsing failed: %s", e)
            return ResumeProfile(raw_text=raw_text)

    # ── Persistence ───────────────────────────────────────────────────────

    def _save_checkpoint(self, profile: ResumeProfile) -> None:
        """Save parsed resume to disk."""
        path = RESUME_OUTPUT_DIR / f"resume_parsed_{self.session_id}.json"
        path.write_text(
            json.dumps(profile.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Checkpoint saved → %s", path)
