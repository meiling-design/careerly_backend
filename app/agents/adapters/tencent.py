"""Tencent careers adapter — uses official public API.

API endpoint discovered via Playwright network capture:
  GET /tencentcareer/api/post/ByPostId?timestamp={ts}&postId={id}&language=zh-cn

No authentication required. Returns full JSON with all fields.
"""

from __future__ import annotations

import re
import time
from typing import Optional

import requests

from app.agents.adapters.base import BaseSiteAdapter
from app.models.job import JobList, JobPosting, JobSource
from app.utils.logger import logger


class TencentAdapter(BaseSiteAdapter):
    """Adapter for careers.tencent.com — uses public JSON API."""

    adapter_name = "tencent"

    # Match: https://careers.tencent.com/jobdesc.html?postId=XXXXX
    URL_PATTERN = re.compile(
        r"https?://careers\.tencent\.com/jobdesc\.html\?.*postId=(\d+)",
        re.IGNORECASE,
    )

    API_TEMPLATE = (
        "https://careers.tencent.com/tencentcareer/api/post/ByPostId"
        "?timestamp={timestamp}&postId={post_id}&language=zh-cn"
    )

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return bool(cls.URL_PATTERN.search(url))

    async def fetch_job(
        self,
        url: str,
        use_playwright: bool = True,
        session_id: Optional[str] = None,
    ) -> list[JobPosting]:
        # Extract postId
        match = self.URL_PATTERN.search(url)
        if not match:
            logger.warning("TencentAdapter: cannot extract postId from %s", url)
            return []

        post_id = match.group(1)
        api_url = self.API_TEMPLATE.format(
            timestamp=int(time.time() * 1000),
            post_id=post_id,
        )

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://careers.tencent.com/",
            "Origin": "https://careers.tencent.com",
        }

        try:
            resp = requests.get(api_url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error("TencentAdapter: API request failed for %s: %s", post_id, e)
            return []

        # Parse response
        result = data.get("Data") or {}
        if not result:
            logger.warning("TencentAdapter: empty Data for postId=%s", post_id)
            return []

        title = result.get("RecruitPostName") or result.get("PostId") or "Unknown Position"
        location = result.get("LocationName")
        bg_name = result.get("BGName", "")
        category = result.get("CategoryName", "")
        product = result.get("ProductName", "")
        company = f"腾讯 ({bg_name})" if bg_name else "腾讯"
        if product:
            company += f" · {product}"

        # Description: combine responsibility + requirement + introduction
        responsibility = self.safe_text(result.get("Responsibility", ""), 4000)
        requirement = self.safe_text(result.get("Requirement", ""), 4000)
        introduction = self.safe_text(result.get("Introduction", ""), 2000)
        description = "\n".join(
            part for part in [
                f"{introduction}" if introduction else "",
                f"【岗位职责】\n{responsibility}" if responsibility else "",
                f"【任职要求】\n{requirement}" if requirement else "",
            ] if part.strip()
        )

        # Extract skills from requirement using heuristics
        required_skills = self._extract_skills(requirement)
        # Experience
        exp_text = result.get("RequireWorkYearsName", "")

        job = JobPosting(
            title=title.strip(),
            company=company,
            location=location,
            description=description[:5000],
            url=url,
            required_skills=required_skills,
            required_experience=self._parse_experience(exp_text),
            responsibilities=self._split_lines(responsibility),
            soft_skills=[],
            source=JobSource.CRAWL,
            crawled_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        )
        logger.info(
            "TencentAdapter: parsed '%s' @ %s | skills=%d",
            title, location or "?", len(required_skills),
        )
        return [job]

    # ── Internal helpers ──────────────────────────────────────────────

    @staticmethod
    def _extract_skills(text: str) -> list[str]:
        """Extract tech/business skills from requirement text."""
        if not text:
            return []

        # Common tech keywords in Chinese job descriptions
        tech_keywords = [
            "Python", "Java", "Go", "Golang", "C++", "Rust", "Scala", "Kotlin",
            "TensorFlow", "PyTorch", "RAG", "Agent", "LLM", "NLP", "AI", "机器学习",
            "深度学习", "大模型", "自然语言处理",
            "MySQL", "Redis", "Kafka", "MongoDB", "PostgreSQL", "Elasticsearch",
            "Docker", "Kubernetes", "K8s", "微服务", "分布式",
            "React", "Vue", "Angular", "TypeScript", "JavaScript",
            "Flask", "FastAPI", "Django", "Spring", "Spring Boot",
            "产品规划", "用户研究", "数据分析", "A/B测试",
            "项目管理", "敏捷开发", "Scrum",
        ]

        found = []
        text_lower = text.lower()
        for kw in tech_keywords:
            if kw.lower() in text_lower:
                found.append(kw)

        # Deduplicate while preserving order
        seen: set[str] = set()
        unique = []
        for s in found:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return unique

    @staticmethod
    def _parse_experience(exp_text: str) -> int | None:
        """Parse years of experience from Chinese text like '三年以上工作经验'."""
        if not exp_text:
            return None
        match = re.search(r"(\d+)", exp_text)
        if match:
            return int(match.group(1))
        if "一年" in exp_text:
            return 1
        if "三年" in exp_text:
            return 3
        if "五年" in exp_text:
            return 5
        return None

    @staticmethod
    def _split_lines(text: str) -> list[str]:
        """Split text into numbered list items."""
        if not text:
            return []
        # Split by number prefix (1. 2. ...) or newline
        lines = re.split(r"\d+[.、．]\s*", text)
        return [l.strip() for l in lines if l.strip()]
