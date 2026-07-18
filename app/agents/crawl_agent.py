"""Crawl Agent — Universal job crawler with site adapters.

Architecture:
  URL → Adapter Registry → Matched Adapter → JobPosting(s)

Built-in adapters:
  - TencentAdapter    careers.tencent.com          (public API)
  - ByteDanceAdapter  jobs.bytedance.com           (Playwright + API)
  - GenericSPAAdapter Known SPA domains             (Playwright + LLM)
  - GenericHTMLAdapter  Any URL                      (requests + BS4)

Usage:
    agent = CrawlAgent()
    jobs = await agent.run(urls=["https://careers.tencent.com/..."])
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Optional

from app.agents.adapters import get_adapter_for_url
from app.models.job import JobList, JobSource
from app.services.llm_service import LLMService
from app.utils.helpers import validate_url
from app.utils.logger import logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CRAWL_OUTPUT_DIR = DATA_DIR / "outputs"


class CrawlAgent:
    """Universal job crawler using site adapters.

    Automatically selects the best adapter for each URL.
    Falls back to generic adapters for unknown sites.
    """

    def __init__(
        self,
        llm: Optional[LLMService] = None,
        use_playwright: bool = True,
        timeout: int = 30,
    ):
        self.llm = llm or LLMService()
        self.use_playwright = use_playwright
        self.timeout = timeout
        self.session_id: str = ""
        CRAWL_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("CrawlAgent initialized | adapters: Tencent, ByteDance, GenericSPA, GenericHTML")

    # ── Main entry point ──────────────────────────────────────────────────

    async def run(
        self,
        urls: list[str],
        use_llm: bool = True,
        session_id: Optional[str] = None,
    ) -> JobList:
        """Crawl a list of URLs and return structured job listings.

        Each URL is dispatched to the matching site adapter.
        Unmatched URLs are skipped with a warning.

        Args:
            urls: List of job posting URLs to crawl.
            use_llm: Whether to use LLM for field enrichment.
            session_id: Optional session ID for traceability.

        Returns:
            JobList with all discovered jobs aggregated across URLs.
        """
        self.session_id = session_id or f"crawl-{uuid.uuid4().hex[:8]}"
        logger.info(
            "CrawlAgent starting | session=%s | urls=%d",
            self.session_id, len(urls),
        )

        valid_urls = [u for u in urls if validate_url(u)]
        if not valid_urls:
            logger.warning("No valid URLs provided")
            return JobList(session_id=self.session_id)

        job_list = JobList(session_id=self.session_id)
        stats: dict[str, int] = {"matched": 0, "unmatched": 0, "errors": 0}

        for url in valid_urls:
            adapter = get_adapter_for_url(url, llm=self.llm)

            if adapter is None:
                logger.warning("No adapter found for %s — skipping", url)
                stats["unmatched"] += 1
                continue

            logger.info(
                "Dispatching %s → %s", url, adapter.adapter_name,
            )

            try:
                jobs = await adapter.fetch_job(
                    url=url,
                    use_playwright=self.use_playwright,
                    session_id=self.session_id,
                )

                if jobs:
                    for job in jobs:
                        job_list.add(job)
                    logger.info(
                        "%s: %d job(s) from %s",
                        adapter.adapter_name, len(jobs), url,
                    )
                    stats["matched"] += 1
                else:
                    logger.warning(
                        "%s: no jobs found from %s",
                        adapter.adapter_name, url,
                    )
                    stats["matched"] += 1  # adapter matched but returned nothing

            except Exception as e:
                logger.error(
                    "%s failed for %s: %s",
                    adapter.adapter_name, url, e,
                )
                stats["errors"] += 1
                continue

        job_list.deduplicate()

        logger.info(
            "CrawlAgent done | session=%s | total=%d | stats=%s",
            self.session_id, job_list.total_count, stats,
        )

        self._save_checkpoint(job_list)
        return job_list

    # ── Persistence ─────────────────────────────────────────────────────

    def _save_checkpoint(self, job_list: JobList) -> None:
        """Save intermediate crawl results to disk."""
        path = CRAWL_OUTPUT_DIR / f"crawl_output_{self.session_id}.json"
        path.write_text(
            json.dumps(
                {
                    "session_id": self.session_id,
                    "jobs": [j.model_dump() for j in job_list.jobs],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("Checkpoint saved → %s", path)
