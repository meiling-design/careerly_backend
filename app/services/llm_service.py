"""LLM Service — unified interface to OpenAI / Claude / DashScope models."""

from __future__ import annotations

import json
import os
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI

from app.utils.logger import logger

load_dotenv()


# ── System prompts (core assets, loaded from inline constants) ──────────────

JOB_PARSE_PROMPT = """你是招聘分析专家，请从以下职位描述中提取结构化信息。输出必须是JSON格式：
{
  "required_skills": ["技能1", "技能2"],
  "preferred_skills": ["加分技能1"],
  "required_experience": 年数(数字),
  "responsibilities": ["职责1", "职责2"],
  "soft_skills": ["沟通能力", "团队协作"]
}
只输出JSON，不要其他文字。"""

RESUME_PARSE_PROMPT = """请将以下简历内容转换为结构化技能图谱。输出必须是JSON格式：
{
  "name": "姓名",
  "email": "邮箱",
  "phone": "电话",
  "skills": ["Python", "AWS"],
  "experience_years": 年数(数字),
  "roles": ["后端工程师"],
  "projects": [
    {"name": "项目名", "description": "描述", "tech_stack": ["FastAPI"], "highlights": ["成果"]}
  ],
  "education": [
    {"degree": "学位", "institution": "学校", "year": 毕业年份}
  ],
  "certifications": ["证书名"]
}
只输出JSON，不要其他文字。"""

MATCH_PROMPT = """你是一名资深的招聘匹配专家。请评估该简历与该职位的匹配程度，输出JSON：
{
  "score": 0.85,
  "match_reasons": ["Python 技能匹配", "有微服务经验"],
  "missing_skills": ["Kubernetes"],
  "partial_skills": ["AWS(基础)"],
  "recommendations": ["补充K8s项目经验"],
  "gaps": [
    {"skill": "Kubernetes", "severity": "missing", "suggestion": "学习K8s基础并做一个小项目"}
  ]
}
评分说明：
- 0.9+：完美匹配
- 0.7-0.9：良好匹配
- 0.5-0.7：部分匹配
- 0.5以下：弱匹配

只输出JSON，不要其他文字。"""


class LLMService:
    """Unified LLM client supporting OpenAI-compatible APIs (including DashScope)."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
    ):
        self.model = model or os.getenv("LLM_MODEL", "qwen3.5-plus")
        self.temperature = temperature
        self.max_tokens = max_tokens

        base_url = base_url or os.getenv("LLM_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL")
        api_key = api_key or os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY")

        if not api_key:
            raise ValueError(
                "No API key found. Set LLM_API_KEY, DASHSCOPE_API_KEY, "
                "or OPENAI_API_KEY in .env"
            )

        self.client = OpenAI(api_key=api_key, base_url=base_url)
        logger.info(
            "LLMService initialized | model=%s | base_url=%s",
            self.model,
            base_url,
        )

    # ── Public interface ──────────────────────────────────────────────────

    def chat(
        self,
        system: str,
        user: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict | None = None,
    ) -> str:
        """Send a chat completion request.

        Args:
            system: System prompt.
            user: User message content.
            temperature: Override default temperature.
            max_tokens: Override default max_tokens.

        Returns:
            Response text.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if response_format:
            kwargs["response_format"] = response_format

        try:
            resp = self.client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
            logger.debug("LLM response (%d chars): %s...", len(text), text[:100])
            return text
        except Exception as e:
            logger.error("LLM chat failed: %s", e)
            raise

    def chat_json(self, system: str, user: str, **kwargs) -> dict:
        """Chat and parse response as JSON."""
        raw = self.chat(system, user, **kwargs)
        return self._safe_json(raw)

    # ── Domain-specific helpers ───────────────────────────────────────────

    def parse_job(self, description: str) -> dict:
        """Extract structured fields from a job description."""
        return self.chat_json(JOB_PARSE_PROMPT, description)

    def parse_resume(self, resume_text: str) -> dict:
        """Extract structured skill graph from resume text."""
        return self.chat_json(RESUME_PARSE_PROMPT, resume_text)

    def match(
        self,
        resume_text: str,
        job_text: str,
        job_title: str = "",
    ) -> dict:
        """Evaluate match between a resume and a job posting."""
        user_prompt = (
            f"【职位】{job_title}\n\n"
            f"【职位描述】\n{job_text}\n\n"
            f"【简历】\n{resume_text}"
        )
        return self.chat_json(MATCH_PROMPT, user_prompt)

    # ── Internals ─────────────────────────────────────────────────────────

    @staticmethod
    def _safe_json(text: str) -> dict:
        """Extract and parse JSON from LLM output (handles markdown fences)."""
        # Strip markdown code fences
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            logger.warning("LLM output is not valid JSON, attempting repair...")
            # Try to find { ... } block
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    pass
            logger.error("Failed to parse LLM output as JSON: %s", text[:300])
            return {}
