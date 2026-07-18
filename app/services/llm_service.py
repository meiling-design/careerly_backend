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
  "title": "职位名称（如 '高级Python工程师'）",
  "company": "公司名称（如能找到）",
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

# ── ⭐ LLM Hybrid Router 提示词 ────────────────────────────────────────

ROUTING_PROMPT = """你是一个决策路由专家。根据输入的数据质量信号，决定工作匹配管线下一步的"最佳动作"。

可选的决策动作：
1. "match"     — 数据充足，进入匹配阶段
2. "recrawl"   — 数据不足但可能通过重试改善（改变抓取策略）
3. "refine_crawl" — 数据质量一般，需要调整爬取方式（如启用JS渲染、尝试不同解析策略）
4. "ask_user"  — 关键数据缺失，需要人工介入
5. "end"       — 无法恢复的错误，终止流程

输出必须是JSON格式：
{
  "next_action": "match | recrawl | refine_crawl | ask_user | end",
  "confidence": 0.0-1.0,
  "reason": "简明扼要的中文原因说明",
  "suggested_strategy": "如果是 refine_crawl，建议的具体策略描述"
}

评估维度和阈值参考：
- resume_skills >= 3 且 jobs >= 1 → match 倾向
- jobs == 0 且 retry_remaining > 0 → recrawl 倾向
- resume 存在但 skills==0 → 可能 ask_user（简历质量差）
- jobs 存在但 quality 差（平均描述短、skills 提取少）→ refine_crawl 倾向
- 有 fatal 错误 → end 倾向

只输出JSON，不要其他文字。"""

# ── ⭐ Batch LLM Matching 提示词 ─────────────────────────────────────

BATCH_MATCH_PROMPT = """你是一名资深的招聘匹配专家。请评估以下简历与多个职位的匹配程度。

对每个职位，从以下维度综合评估：
1. 技能重合度（必备/加分技能匹配情况）
2. 经验匹配度（年限、行业背景）
3. 综合匹配度
4. 缺点/差距分析

输出必须是JSON数组格式，每个元素对应一个职位：
[
  {
    "job_index": 0,
    "score": 0.85,
    "match_reasons": ["Python 技能匹配", "有微服务经验"],
    "missing_skills": ["Kubernetes"],
    "gaps": [
      {"skill": "Kubernetes", "severity": "missing", "suggestion": "学习K8s基础并做一个小项目"}
    ],
    "recommendations": ["补充K8s项目经验"]
  },
  ...
]

评分说明：
- 0.9+：完美匹配
- 0.7-0.9：良好匹配
- 0.5-0.7：部分匹配
- 0.5以下：弱匹配

只输出JSON数组，不要其他文字。"""


class LLMService:
    """Unified LLM client supporting OpenAI-compatible APIs (including DashScope)."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        timeout: int = 120,
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

        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
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

    # ── ⭐ LLM Hybrid Router ──────────────────────────────────────────────

    def route(self, quality_signals: dict) -> dict:
        """LLM-based routing decision using data quality signals.

        Args:
            quality_signals: Dict with keys like:
                - total_jobs, avg_job_desc_length, avg_skills_per_job
                - resume_exists, resume_skills_count, resume_has_raw_text
                - crawl_error_count, recrawl_remaining, fatal_errors

        Returns:
            Dict with next_action, confidence, reason, suggested_strategy.
        """
        user_prompt = (
            f"请根据以下数据质量信号做出路由决策：\n\n"
            f"{json.dumps(quality_signals, ensure_ascii=False, indent=2)}"
        )
        return self.chat_json(ROUTING_PROMPT, user_prompt)

    # ── ⭐ Batch LLM Matching ─────────────────────────────────────────────

    def batch_match(
        self,
        resume_text: str,
        jobs: list[dict],
    ) -> list[dict]:
        """Batch evaluate a resume against multiple jobs in one LLM call.

        Args:
            resume_text: Formatted resume text.
            jobs: List of dicts with keys: job_index, title, description.

        Returns:
            List of dicts with job_index, score, match_reasons, etc.
        """
        user_prompt = (
            f"【简历】\n{resume_text}\n\n"
            f"【职位列表】\n{json.dumps(jobs, ensure_ascii=False, indent=2)}"
        )
        raw = self.chat(BATCH_MATCH_PROMPT, user_prompt)
        return self._safe_json_array(raw)

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

    @staticmethod
    def _safe_json_array(text: str) -> list[dict]:
        """Extract and parse a JSON array from LLM output."""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            text = text.rsplit("```", 1)[0]
        text = text.strip()

        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
            # Sometimes LLM wraps array in an object
            if isinstance(result, dict):
                for key in ("results", "matches", "data", "items"):
                    if key in result and isinstance(result[key], list):
                        return result[key]
            return []
        except json.JSONDecodeError:
            logger.warning("LLM output is not valid JSON array, attempting repair...")
            start = text.find("[")
            end = text.rfind("]")
            if start != -1 and end != -1:
                try:
                    result = json.loads(text[start : end + 1])
                    return result if isinstance(result, list) else []
                except json.JSONDecodeError:
                    pass
            logger.error("Failed to parse LLM output as JSON array: %s", text[:300])
            return []
