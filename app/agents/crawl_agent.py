"""Crawl Agent — fetch job listings from URLs.

Supports:
- Static pages via requests + BeautifulSoup
- Dynamic pages via Playwright (auto-fallback)
- Text extraction via trafilatura
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from app.models.job import JobList, JobPosting, JobSource
from app.services.llm_service import LLMService
from app.utils.helpers import normalize_text, validate_url
from app.utils.logger import logger

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CRAWL_OUTPUT_DIR = DATA_DIR / "outputs"


class CrawlAgent:
    """Fetch and parse job listings from URLs."""

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

    # ── Main entry point ──────────────────────────────────────────────────

    async def run(
        self,
        urls: list[str],
        use_llm: bool = True,
        session_id: Optional[str] = None,
    ) -> JobList:
        """Crawl a list of URLs and return structured job listings.

        Args:
            urls: List of job posting URLs to crawl.
            use_llm: Whether to enhance raw text with LLM parsing.
            session_id: Optional session ID for traceability.

        Returns:
            JobList with all discovered jobs.
        """
        self.session_id = session_id or f"crawl-{uuid.uuid4().hex[:8]}"
        logger.info("CrawlAgent starting | session=%s | urls=%s", self.session_id, len(urls))

        valid_urls = [u for u in urls if validate_url(u)]
        if not valid_urls:
            logger.warning("No valid URLs provided")
            return JobList(session_id=self.session_id)

        job_list = JobList(session_id=self.session_id)

        for url in valid_urls:
            try:
                raw_html = await self._fetch(url)
                if not raw_html:
                    continue

                text = self._extract_text(raw_html)
                if not text or len(text.strip()) < 50:
                    logger.warning("Insufficient content from %s (%d chars)", url, len(text or ""))
                    continue

                if use_llm:
                    jobs = self._parse_with_llm(text, url)
                else:
                    jobs = self._parse_heuristic(text, url)

                for job in jobs:
                    job_list.add(job)

                logger.info("Crawled %s → %d jobs", url, len(jobs))

            except Exception as e:
                logger.error("Failed to crawl %s: %s", url, e)
                continue

        job_list.deduplicate()
        self._save_checkpoint(job_list)
        logger.info("CrawlAgent done | total_jobs=%d", job_list.total_count)
        return job_list

    # ── Fetching ──────────────────────────────────────────────────────────

    async def _fetch(self, url: str) -> Optional[str]:
        """Fetch HTML from a URL. Uses requests first, falls back to Playwright."""
        # Try static first
        html = self._fetch_static(url)
        if html and self._is_content_rich(html):
            return html

        # Fallback to Playwright
        if self.use_playwright:
            logger.info("Static fetch insufficient, trying Playwright: %s", url)
            try:
                html = await self._fetch_dynamic(url)
            except Exception as e:
                logger.warning("Playwright failed for %s: %s", url, e)
                return html  # return static content even if thin

        return html

    def _fetch_static(self, url: str) -> Optional[str]:
        """Fetch via requests + BeautifulSoup."""
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            }
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            logger.debug("Fetched static %s (%d bytes)", url, len(resp.text))
            return resp.text
        except requests.RequestException as e:
            logger.warning("Static fetch failed for %s: %s", url, e)
            return None

    async def _fetch_dynamic(self, url: str) -> Optional[str]:
        """Fetch via Playwright for JavaScript-rendered pages."""
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    )
                )
                await page.goto(url, wait_until="networkidle", timeout=self.timeout * 1000)
                html = await page.content()
                await browser.close()
                logger.debug("Fetched dynamic %s (%d bytes)", url, len(html))
                return html
        except ImportError:
            logger.warning("Playwright not installed, dynamic fetch unavailable")
            return None
        except Exception as e:
            logger.error("Dynamic fetch error for %s: %s", url, e)
            return None

    # ── Text Extraction ───────────────────────────────────────────────────

    def _extract_text(self, html: str) -> str:
        """Extract clean text from HTML. Uses trafilatura if available."""
        try:
            import trafilatura  # type: ignore[import-untyped]
            text = trafilatura.extract(html, include_links=True, include_tables=False)
            if text and len(text) > 100:
                return normalize_text(text)
        except ImportError:
            pass

        # Fallback: BeautifulSoup text extraction
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return normalize_text(text)

    # ── Parsing ───────────────────────────────────────────────────────────

    def _parse_with_llm(self, text: str, url: str) -> list[JobPosting]:
        """Use LLM to extract structured job info from text."""
        try:
            result = self.llm.parse_job(text)
            if not result:
                return self._parse_heuristic(text, url)

            job = JobPosting(
                title=result.get("title") or result.get("required_skills", ["Unknown"])[0] or "Unknown Position",
                description=text[:3000],
                url=url,
                company=self._guess_company(text, url),
                required_skills=result.get("required_skills", []),
                preferred_skills=result.get("preferred_skills", []),
                required_experience=result.get("required_experience"),
                responsibilities=result.get("responsibilities", []),
                soft_skills=result.get("soft_skills", []),
                source=JobSource.CRAWL,
                crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            return [job]
        except Exception as e:
            logger.warning("LLM parse failed, falling back to heuristic: %s", e)
            return self._parse_heuristic(text, url)

    def _parse_heuristic(self, text: str, url: str) -> list[JobPosting]:
        """Basic regex-based parsing without LLM."""
        text_clean = normalize_text(text, max_len=5000)

        # Try to extract title from common patterns
        title = "Unknown Position"
        title_patterns = [
            r"(?:职位|岗位|Job Title|Position)[：:\s]*([^\n]{2,40})",
            r"<title>([^<]+)</title>",
        ]
        for pat in title_patterns:
            m = re.search(pat, text_clean, re.IGNORECASE)
            if m:
                title = m.group(1).strip()
                break

        company = self._guess_company(text_clean, url)

        return [
            JobPosting(
                title=title,
                description=text_clean[:3000],
                url=url,
                company=company,
                source=JobSource.CRAWL,
                crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
        ]

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _guess_company(text: str, url: str) -> Optional[str]:
        """Guess company name from URL or text."""
        # Try from URL
        m = re.search(r"https?://([^.]+)\.(?:com|cn|jobs?|careers?)", url)
        if m:
            return m.group(1).title()

        # Try from text
        for keyword in ["公司", "Company", "企业"]:
            m = re.search(rf"{keyword}[：:\s]*([^\n]{{2,30}})", text)
            if m:
                return m.group(1).strip()
        return None

    @staticmethod
    def _is_content_rich(html: str) -> bool:
        """Check if the HTML has meaningful job content."""
        if not html or len(html) < 500:
            return False
        # Look for job-related keywords
        keywords = ["job", "职位", "招聘", "description", "responsibility", "requirement",
                     "经验", "技能", "岗位"]
        text = BeautifulSoup(html, "html.parser").get_text().lower()
        return any(k.lower() in text for k in keywords)

    def _save_checkpoint(self, job_list: JobList) -> None:
        """Save intermediate crawl results to disk."""
        path = CRAWL_OUTPUT_DIR / f"crawl_output_{self.session_id}.json"
        path.write_text(
            json.dumps(
                {"session_id": self.session_id, "jobs": [j.model_dump() for j in job_list.jobs]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("Checkpoint saved → %s", path)
