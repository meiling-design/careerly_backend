"""ByteDance careers adapter — Playwright-based with known API patterns.

ByteDance's career portal (jobs.bytedance.com) is a complex React SPA.
The position detail page loads data via JavaScript after initial render.

Strategy (fallback chain):
  1. Try known internal API endpoints (fast, but may be blocked)
  2. Use Playwright to render the page and extract text (reliable)
  3. LLM-enhanced extraction from rendered text (fallback)

Known API patterns (may change over time):
  - GET /api/v1/search/job/posts          (job listing)
  - GET /api/v1/job/detail/{id}           (detail, returns SPA shell)
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

from app.agents.adapters.base import BaseSiteAdapter
from app.models.job import JobPosting, JobSource
from app.utils.logger import logger


class ByteDanceAdapter(BaseSiteAdapter):
    """Adapter for jobs.bytedance.com — Playwright + API fallback."""

    adapter_name = "bytedance"

    # Match: https://jobs.bytedance.com/experienced/position/XXXXXXXXXXXX
    URL_PATTERN = re.compile(
        r"https?://jobs\.bytedance\.com/.+?/position/(\d+)",
        re.IGNORECASE,
    )

    # Known API endpoints to try first
    API_ENDPOINTS = [
        "https://jobs.bytedance.com/api/v1/job/detail/{job_id}",
        "https://jobs.bytedance.com/api/v1/position/detail/{job_id}",
    ]

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(cls.URL_PATTERN.search(url))

    async def fetch_job(
        self,
        url: str,
        use_playwright: bool = True,
        session_id: Optional[str] = None,
    ) -> list[JobPosting]:
        match = self.URL_PATTERN.search(url)
        if not match:
            logger.warning("ByteDanceAdapter: cannot extract job_id from %s", url)
            return []

        job_id = match.group(1)

        # Step 1: Try known API endpoints (fast path)
        jobs = await self._try_api(job_id, url)
        if jobs:
            return jobs

        # Step 2: Use Playwright to render and extract
        if use_playwright:
            try:
                jobs = await self._try_playwright(url, job_id)
                if jobs:
                    return jobs
            except Exception as e:
                logger.warning("ByteDanceAdapter: Playwright failed: %s", e)

        logger.warning("ByteDanceAdapter: all methods exhausted for %s", url)
        return []

    async def _try_api(self, job_id: str, source_url: str) -> list[JobPosting]:
        """Try known ByteDance API endpoints."""
        import requests

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://jobs.bytedance.com/",
            "Accept": "application/json, text/plain, */*",
        }

        for template in self.API_ENDPOINTS:
            api_url = template.format(job_id=job_id)
            try:
                resp = requests.get(api_url, headers=headers, timeout=10)
                ct = resp.headers.get("Content-Type", "")
                if "json" not in ct:
                    continue
                data = resp.json()
                if isinstance(data, dict) and len(data) > 5:
                    jobs = self._parse_api_response(data, source_url)
                    if jobs:
                        logger.info(
                            "ByteDanceAdapter: API hit for %s", api_url
                        )
                        return jobs
            except Exception:
                continue
        return []

    async def _try_playwright(
        self, url: str, job_id: str
    ) -> list[JobPosting]:
        """Use Playwright to render the page and extract job data."""
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(4)  # Wait for JS rendering

                # Try to get window.__NUXT__ or __INITIAL_STATE__
                state = await page.evaluate("""() => {
                    return window.__NUXT__ || window.__INITIAL_STATE__ || null;
                }""")
                if state:
                    jobs = self._parse_state(state, url)
                    if jobs:
                        await browser.close()
                        return jobs

                # Fallback: extract visible text
                body_text = await page.inner_text("body")
                title = await page.title()
                await browser.close()

                if len(body_text) > 200:
                    return self._from_text(body_text, title, url)
                return []

        except ImportError:
            logger.warning("ByteDanceAdapter: playwright not installed")
            return []
        except Exception as e:
            logger.warning("ByteDanceAdapter: Playwright error: %s", e)
            return []

    def _parse_api_response(
        self, data: dict[str, Any], source_url: str
    ) -> list[JobPosting]:
        """Parse JSON from ByteDance's internal API."""
        # The API response structure varies — try common paths
        job_data = (
            data.get("data")
            or data.get("Data")
            or data.get("result")
            or data.get("Result")
            or data
        )
        if isinstance(job_data, dict):
            return [self._build_job(job_data, source_url)]
        return []

    def _parse_state(
        self, state: dict[str, Any], source_url: str
    ) -> list[JobPosting]:
        """Parse __NUXT__ or __INITIAL_STATE__."""
        # Deep search for job-like data
        def find_job_data(obj: Any, depth: int = 0) -> dict | None:
            if depth > 5 or not isinstance(obj, dict):
                return None
            # Look for position/job related keys
            keys = set(obj.keys())
            if keys & {"positionId", "jobId", "postId", "PositionId", "JobId"}:
                return obj
            for v in obj.values():
                result = find_job_data(v, depth + 1)
                if result:
                    return result
            return None

        job_data = find_job_data(state)
        if job_data:
            return [self._build_job(job_data, source_url)]
        return []

    def _from_text(
        self, text: str, title: str, source_url: str
    ) -> list[JobPosting]:
        """Build a basic job posting from rendered page text."""
        company = "字节跳动"
        # Try to extract position name from title
        pos_title = title if title and "招聘" not in title else "Position"

        return [
            JobPosting(
                title=pos_title.strip(),
                description=self.safe_text(text, 5000),
                url=source_url,
                company=company,
                source=JobSource.CRAWL,
                crawled_at=__import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
            )
        ]

    def _build_job(
        self, data: dict[str, Any], source_url: str
    ) -> JobPosting:
        """Build a JobPosting from a parsed data dict (works across API & state)."""
        # Normalise field names (camelCase / PascalCase / snake_case)
        def get(*keys: str) -> Any:
            for k in keys:
                v = data.get(k)
                if v is not None:
                    return v
            return None

        title = (
            get("positionName", "PositionName", "RecruitPostName",
                "jobName", "JobName", "title", "Title")
            or "Unknown Position"
        )
        description = (
            get("description", "Description", "jobDescription", "JobDescription",
                "requirement", "Requirement", "responsibility", "Responsibility")
            or ""
        )
        company = (
            get("company", "Company", "companyName", "CompanyName", "brandName")
            or "字节跳动"
        )
        location = (
            get("location", "Location", "city", "City", "locationName", "LocationName")
            or None
        )
        skills = (
            get("skills", "Skills", "requiredSkills", "RequiredSkills",
                "skillList", "SkillList")
            or []
        )
        experience = (
            get("experience", "Experience", "workYears", "WorkYears",
                "requireWorkYears", "RequireWorkYears")
            or None
        )

        if isinstance(experience, str):
            exp_match = re.search(r"(\d+)", experience)
            experience = int(exp_match.group(1)) if exp_match else None

        return JobPosting(
            title=str(title).strip(),
            company=str(company).strip(),
            location=str(location).strip() if location else None,
            description=self.safe_text(str(description), 5000),
            url=source_url,
            required_skills=list(skills) if isinstance(skills, list) else [],
            required_experience=experience,
            source=JobSource.CRAWL,
            crawled_at=__import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        )
