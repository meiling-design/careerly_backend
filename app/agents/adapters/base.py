"""Base adapter interface for job platform crawling."""

from __future__ import annotations

from typing import Optional

from app.models.job import JobList, JobPosting


class BaseSiteAdapter:
    """Abstract base for a job platform adapter.

    Each subclass implements:
      - can_handle(url)   → bool (classmethod)
      - fetch_job(url, use_playwright, session_id)  → list[JobPosting]

    Adapters are tried in priority order; the first match wins.
    """

    # Human-readable name (for logs / debugging)
    adapter_name: str = "generic"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Return True if this adapter knows how to crawl *url*."""
        raise NotImplementedError

    async def fetch_job(
        self,
        url: str,
        use_playwright: bool = True,
        session_id: Optional[str] = None,
    ) -> list[JobPosting]:
        """Fetch one or more job postings from *url*.

        Returns a (possibly empty) list of JobPosting objects.
        May use LLM for structural extraction when the adapter
        gets raw text rather than structured JSON.
        """
        raise NotImplementedError

    # ── Shared helpers ────────────────────────────────────────────────

    @staticmethod
    def safe_text(text: str | None, max_len: int = 8000) -> str:
        """Normalise and truncate text for embedding / LLM."""
        if not text:
            return ""
        text = " ".join(text.split())
        return text[:max_len]
