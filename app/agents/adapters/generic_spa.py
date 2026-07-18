"""Generic SPA adapter — Playwright-based fallback for single-page apps.

When no specific adapter matches and the page appears to be a JS-rendered
SPA, this adapter uses Playwright to render the page fully, then extracts
visible text and (optionally) enriches it with LLM parsing.

This is the second-to-last fallback (before GenericHTMLAdapter).
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from app.agents.adapters.base import BaseSiteAdapter
from app.models.job import JobPosting, JobSource
from app.services.llm_service import LLMService
from app.utils.helpers import normalize_text
from app.utils.logger import logger


class GenericSPAAdapter(BaseSiteAdapter):
    """Fallback adapter for unknown SPAs — uses Playwright + LLM.

    This adapter handles sites that are clearly JS-rendered (like
    talent.alibaba.com, zhipin.com, lagou.com, etc.) but don't have
    a dedicated adapter yet.
    """

    adapter_name = "generic_spa"

    # Known SPA domains that should NOT go through the HTML adapter
    SPA_DOMAINS = [
        "talent.alibaba.com",
        "zhipin.com",
        "lagou.com",
        "liepin.com",
        "maimai.cn",
        "51job.com",
        "mkt.51job.com",
        "ciwei.51job.com",
        "shixiseng.com",
        "dajie.com",
    ]

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """Handle known SPA domains or any URL that looks like a job page."""
        url_lower = url.lower()
        for domain in cls.SPA_DOMAINS:
            if domain in url_lower:
                return True
        # Also match common job URL patterns
        if re.search(r"/job|/position|/recruit|/career", url_lower):
            return True
        return False

    def __init__(self, llm: Optional[LLMService] = None):
        self.llm = llm or LLMService()

    async def fetch_job(
        self,
        url: str,
        use_playwright: bool = True,
        session_id: Optional[str] = None,
    ) -> list[JobPosting]:
        if not use_playwright:
            logger.debug("GenericSPA: Playwright disabled, skipping %s", url)
            return []

        try:
            return await self._fetch_with_playwright(url)
        except Exception as e:
            logger.error("GenericSPA: fetch failed for %s: %s", url, e)
            return []

    async def _fetch_with_playwright(self, url: str) -> list[JobPosting]:
        """Load page with Playwright, extract rendered text."""
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.warning("GenericSPA: playwright not installed")
            return []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
            except Exception:
                # Some SPAs never reach networkidle (long-polling, etc.)
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                except Exception as e:
                    logger.warning("GenericSPA: goto failed: %s", e)
                    await browser.close()
                    return []

            # Wait for JS to render content
            await asyncio.sleep(4)

            title = await page.title()
            body_text = await page.inner_text("body")

            # Try to find job-related API responses
            job_data = await self._intercept_api_data(page)
            if job_data:
                await browser.close()
                return self._parse_api_data(job_data, url)

            # Check for __NUXT__/__INITIAL_STATE__ patterns
            state = await page.evaluate("""() => {
                return window.__NUXT__ || window.__INITIAL_STATE__
                    || window.__INITIALS__ || window.__DATA__ || null;
            }""")

            await browser.close()

            if state and isinstance(state, dict):
                jobs = self._find_jobs_in_state(state, url)
                if jobs:
                    return jobs

            # Last resort: use rendered text
            if len(body_text) > 200:
                return self._from_text(body_text, title, url)

            logger.warning("GenericSPA: no content extracted from %s", url)
            return []

    async def _intercept_api_data(self, page) -> dict | None:
        """Try to capture the last XHR response with job data."""
        # This is a simplified approach — for production you'd use
        # route interception. Here we just check the page's current state.
        return None

    def _parse_api_data(
        self, data: dict, source_url: str
    ) -> list[JobPosting]:
        """Parse captured API response into JobPosting."""
        # Generic field mapping — works for many Chinese job APIs
        def get(*keys: str) -> str | None:
            for k in keys:
                v = data.get(k) if isinstance(data, dict) else None
                if v is not None:
                    return str(v)
            return None

        title = get(
            "title", "Title", "name", "Name",
            "positionName", "PositionName",
            "jobName", "JobName",
        ) or "Unknown Position"

        desc = "\n".join(filter(None, [
            get("description", "Description", "jobDescription", "JobDescription",
                "content", "Content", "detail", "Detail"),
            get("requirement", "Requirement", "requirements", "Requirements"),
            get("responsibility", "Responsibility", "duty", "Duty"),
        ]))

        return [
            JobPosting(
                title=title,
                description=self.safe_text(desc, 5000),
                url=source_url,
                company=get("company", "Company", "brandName", "BrandName", "orgName"),
                location=get("location", "Location", "city", "City", "address"),
                source=JobSource.CRAWL,
                crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
        ]

    def _find_jobs_in_state(
        self, state: dict, source_url: str
    ) -> list[JobPosting]:
        """Deep-search for job data in __NUXT__ / __INITIAL_STATE__."""
        def search(obj, depth=0) -> list[dict]:
            if depth > 6:
                return []
            if not isinstance(obj, dict):
                return []
            # Check if THIS dict is a job posting
            if isinstance(obj.get("title"), str) or isinstance(obj.get("postId"), str):
                if obj.get("description") or obj.get("requirement"):
                    return [obj]
            # Recurse
            results = []
            for v in obj.values():
                results.extend(search(v, depth + 1))
            return results

        jobs_data = search(state)
        results = []
        for jd in jobs_data:
            results.append(JobPosting(
                title=str(jd.get("title", jd.get("postId", "Unknown"))),
                description=self.safe_text(
                    str(jd.get("description", "") or jd.get("requirement", "")), 5000
                ),
                url=source_url,
                company=str(jd.get("company", "") or jd.get("orgName", "")) or None,
                location=str(jd.get("location", "") or jd.get("city", "")) or None,
                source=JobSource.CRAWL,
                crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            ))
        return results

    def _from_text(
        self, text: str, title: str, source_url: str
    ) -> list[JobPosting]:
        """Build basic job posting from rendered text."""
        # Try LLM enhancement
        if len(text) > 200:
            try:
                result = self.llm.parse_job(text[:4000])
                if result and result.get("required_skills"):
                    return [
                        JobPosting(
                            title=result.get("title")
                            or title.replace("招聘", "").strip()
                            or "Unknown Position",
                            description=text[:5000],
                            url=source_url,
                            required_skills=result.get("required_skills", []),
                            preferred_skills=result.get("preferred_skills", []),
                            required_experience=result.get("required_experience"),
                            responsibilities=result.get("responsibilities", []),
                            soft_skills=result.get("soft_skills", []),
                            source=JobSource.CRAWL,
                            crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                        )
                    ]
            except Exception:
                pass

        return [
            JobPosting(
                title=title.replace("招聘", "").strip() or "Unknown Position",
                description=self.safe_text(text, 5000),
                url=source_url,
                source=JobSource.CRAWL,
                crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
        ]
