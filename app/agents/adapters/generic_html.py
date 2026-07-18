"""Generic HTML adapter — requests + BeautifulSoup fallback.

Handles traditional server-rendered HTML pages.
Uses the original CrawlAgent logic: requests → BS4 → LLM parsing.
"""

from __future__ import annotations

import re
import time
from typing import Optional

import requests
from bs4 import BeautifulSoup

from app.agents.adapters.base import BaseSiteAdapter
from app.models.job import JobPosting, JobSource
from app.services.llm_service import LLMService
from app.utils.helpers import normalize_text, validate_url
from app.utils.logger import logger


class GenericHTMLAdapter(BaseSiteAdapter):
    """Fallback adapter for traditional HTML pages (requests + BS4 + LLM)."""

    adapter_name = "generic_html"

    # Always can handle — it's the last resort
    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(validate_url(url))

    def __init__(self, llm: Optional[LLMService] = None):
        self.llm = llm or LLMService()

    async def fetch_job(
        self,
        url: str,
        use_playwright: bool = True,
        session_id: Optional[str] = None,
    ) -> list[JobPosting]:
        """Fetch via requests, then try Playwright fallback for SPAs."""
        jobs = self._fetch_static(url)
        if jobs:
            return jobs

        # Static fetch failed — try Playwright
        if use_playwright:
            try:
                jobs = await self._fetch_dynamic(url)
                if jobs:
                    return jobs
            except Exception as e:
                logger.warning("GenericHTML: Playwright failed for %s: %s", url, e)

        return []

    # ── Static fetch (requests + BS4) ─────────────────────────────────

    def _fetch_static(self, url: str) -> list[JobPosting]:
        """Fetch via requests + BeautifulSoup."""
        html = self._get_html(url)
        if not html:
            return []

        text = self._extract_text(html)
        if not text or len(text.strip()) < 50:
            return []

        return self._parse_text(text, url)

    def _get_html(self, url: str) -> str | None:
        """Download HTML via requests."""
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
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"

            # Check if it's an SPA shell (HTML too small for content)
            if self._is_spa_shell(resp.text):
                logger.debug("GenericHTML: %s looks like SPA shell", url)
                return None

            return resp.text
        except requests.RequestException as e:
            logger.warning("GenericHTML: static fetch failed %s: %s", url, e)
            return None

    @staticmethod
    def _is_spa_shell(html: str) -> bool:
        """Detect SPA shell pages that have no real job content."""
        if not html or len(html) < 500:
            return True
        # If HTML is mostly <script>/<link> tags with minimal text, it's likely SPA
        text_len = len(BeautifulSoup(html, "html.parser").get_text(strip=True))
        return text_len < 100

    @staticmethod
    def _extract_text(html: str) -> str:
        """Extract clean text from HTML, using trafilatura if available."""
        try:
            import trafilatura  # type: ignore[import-untyped]
            text = trafilatura.extract(html, include_links=True, include_tables=False)
            if text and len(text) > 100:
                return normalize_text(text)
        except ImportError:
            pass

        # Fallback: BS4
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "aside", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        return normalize_text(text)

    def _parse_text(self, text: str, url: str) -> list[JobPosting]:
        """Try LLM parsing first, fall back to heuristic."""
        try:
            result = self.llm.parse_job(text[:4000])
            if result and result.get("required_skills"):
                return self._llm_to_job(result, text, url)
        except Exception:
            pass

        return self._heuristic_to_job(text, url)

    def _llm_to_job(
        self, result: dict, raw_text: str, url: str
    ) -> list[JobPosting]:
        """Convert LLM parse result to JobPosting list."""
        job = JobPosting(
            title=(
                result.get("title")
                or (result.get("required_skills") or ["Unknown"])[0]
                or "Unknown Position"
            ),
            description=raw_text[:3000],
            url=url,
            company=self._guess_company(raw_text, url),
            required_skills=result.get("required_skills", []),
            preferred_skills=result.get("preferred_skills", []),
            required_experience=result.get("required_experience"),
            responsibilities=result.get("responsibilities", []),
            soft_skills=result.get("soft_skills", []),
            source=JobSource.CRAWL,
            crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        return [job]

    def _heuristic_to_job(self, text: str, url: str) -> list[JobPosting]:
        """Basic regex-based parsing."""
        text_clean = normalize_text(text, max_len=5000)
        title = "Unknown Position"
        patterns = [
            r"(?:职位|岗位|Job Title|Position)[：:\s]*([^\n]{2,40})",
            r"<title>([^<]+)</title>",
        ]
        for pat in patterns:
            m = re.search(pat, text_clean, re.IGNORECASE)
            if m:
                title = m.group(1).strip()
                break

        return [
            JobPosting(
                title=title,
                description=text_clean[:3000],
                url=url,
                company=self._guess_company(text_clean, url),
                source=JobSource.CRAWL,
                crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            )
        ]

    # ── Dynamic fetch (Playwright fallback) ────────────────────────────

    async def _fetch_dynamic(self, url: str) -> list[JobPosting]:
        """Fetch via Playwright for JS-rendered pages."""
        try:
            from playwright.async_api import async_playwright

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(url, wait_until="networkidle", timeout=30000)
                html = await page.content()
                await browser.close()

            text = self._extract_text(html)
            if text and len(text.strip()) > 100:
                return self._parse_text(text, url)
            return []
        except ImportError:
            logger.warning("GenericHTML: playwright not installed")
            return []
        except Exception as e:
            logger.error("GenericHTML: Playwright error: %s", e)
            return []

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _guess_company(text: str, url: str) -> str | None:
        m = re.search(r"https?://([^.]+)\.(?:com|cn|jobs?|careers?)", url)
        if m:
            name = m.group(1).title()
            # Map common URL prefixes to proper company names
            mapping = {
                "Zhaopin": "智联招聘",
                "Lagou": "拉勾",
                "Liepin": "猎聘",
                "Bosszhipin": "Boss直聘",
                "Maimai": "脉脉",
            }
            return mapping.get(name, name)
        for keyword in ["公司", "Company", "企业"]:
            m = re.search(rf"{keyword}[：:\s]*([^\n]{{2,30}})", text)
            if m:
                return m.group(1).strip()
        return None
