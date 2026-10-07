"""LLM Service — unified interface to OpenAI / Claude / DashScope models."""

from __future__ import annotations

import json
import os
from typing import Any, Iterator, Optional

from dotenv import load_dotenv
from openai import OpenAI

from app.utils.logger import logger
from app.utils.redact import structural_preview

load_dotenv()


# ── Provider tuning ─────────────────────────────────────────────────────────

#: 关闭推理链的 ``extra_body``（DashScope / Qwen 系列支持）。
#:
#: ``qwen3.5-plus`` 是**推理模型**，默认会先生成一整段思考链再给答案。
#: 结构化**抽取**（简历 / JD → JSON）是确定性变换，不需要思考链 ——
#: 开着它只会白白生成上千 reasoning token，把延迟推到分钟级。
#:
#: 实测（同一份简历，2026-10-03）：
#:   开推理 199.2s / 3363 completion token
#:   关推理   9.0s /  362 completion token，结构化输出逐字段一致
_NO_THINKING: dict[str, Any] = {"enable_thinking": False}


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

# ── ⭐ Careerly 能力评分提示词（C4：能力分值模型） ────────────────────

CAPABILITY_SCORING_PROMPT = """你是职业能力评估专家。根据候选人的简历画像与目标岗位JD，提炼候选人的核心能力维度并打分。

要求：
- 提炼 4-6 个能力维度，维度名用中文（如「后端架构」「数据库与存储」「商业化经验」）。
  维度名必须是能力领域，不要用具体技能名（如 Python）；
- 每个维度给出 yours（候选人当前水平 0-100）与 target（目标岗位期望水平 0-100）；
- why：一句话说明评分依据，必须引用简历中的具体经历或明确指出缺失；
- actions：1-3 条具体可执行的提升行动建议。

输出必须是JSON格式：
{
  "capabilities": [
    {"name": "能力维度", "yours": 68, "target": 82, "why": "…", "actions": ["…"]}
  ]
}
只输出JSON，不要其他文字。"""


class LLMService:
    """Unified LLM client supporting OpenAI-compatible APIs (including DashScope)."""

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float = 0.1,
        max_tokens: int = 8192,
        timeout: int = 120,
    ):
        self.model = model or os.getenv("LLM_MODEL", "qwen3.5-plus")
        self.temperature = temperature
        self.max_tokens = max_tokens
        # Whether the provider honours response_format={"type": "json_object"}.
        # None = not tried yet. Once rejected we remember False so we don't
        # pay for a doomed 400 on every single call.
        self._json_mode: bool | None = None

        # Whether the provider honours extra_body={"enable_thinking": False}.
        # Same memoised-probe pattern as _json_mode: a non-Qwen provider would
        # reject the unknown argument, and we must not pay that 400 per call.
        self._no_thinking: bool | None = None

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
        extra_body: dict | None = None,
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
        if extra_body:
            kwargs["extra_body"] = extra_body

        try:
            resp = self.client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or ""
            # ⚠️ 模型输出必须脱敏（TODO K21）：内容来自简历 / JD。
            # 骨架保留了结构信息（围栏 / 截断 / 转义），够排查用。
            logger.debug(
                "LLM response (%d chars): %s", len(text), structural_preview(text, 100)
            )
            return text
        except Exception as e:
            logger.error("LLM chat failed: %s", e)
            raise

    def chat_stream(
        self,
        system: str,
        user: str,
        history: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """Stream a chat completion, yielding text deltas as they arrive.

        Used by the Careerly AI advisor (``POST /api/v1/ai/messages``), which
        relays every delta to the client as an SSE event so the answer types
        itself out rather than appearing all at once.

        ⚠️ Only ``delta.content`` is yielded. ``qwen3.5-plus`` is a reasoning
        model and also emits ``reasoning_content`` — that is the model thinking
        out loud, and relaying it would leak the chain of thought into the UI.

        ``history`` is the prior conversation (``[{"role", "content"}, …]``),
        supplied by the client because conversation storage is client-side
        (裁决 2026-10-03 ②). It is clamped here so a long chat cannot silently
        blow up the token budget.
        """
        messages: list[dict] = [{"role": "system", "content": system}]
        for turn in (history or [])[-self.MAX_HISTORY_TURNS:]:
            role = turn.get("role")
            content = turn.get("content")
            # Silently drop anything that is not a plain user/assistant turn:
            # a malformed role would be rejected by the API, and a "system"
            # turn coming from the client is exactly the injection vector this
            # boundary exists to close.
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content[: self.MAX_HISTORY_CHARS]})
        messages.append({"role": "user", "content": user})

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature or self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
            "stream": True,
        }

        try:
            stream = self.client.chat.completions.create(**kwargs)
            for chunk in stream:
                if not chunk.choices:
                    continue
                text = getattr(chunk.choices[0].delta, "content", None)
                if text:
                    yield text
        except Exception as e:
            logger.error("LLM stream failed: %s", e)
            raise

    #: Streaming context bounds — implementation limits, not product rules.
    MAX_HISTORY_TURNS = 20
    MAX_HISTORY_CHARS = 4000

    def chat_json(self, system: str, user: str, **kwargs) -> dict:
        """Chat and parse the response as a JSON object.

        Prefers the provider's JSON mode: constraining the decoder beats
        repairing malformed output after the fact. If the provider rejects
        ``response_format`` we fall back to a plain call and remember it, so
        the penalty is paid at most once per process.
        """
        if self._json_mode is not False:
            try:
                raw = self.chat(
                    system, user, response_format={"type": "json_object"}, **kwargs
                )
                self._json_mode = True
                return self._safe_json(raw)
            except Exception as exc:
                if not self._json_mode_unsupported(exc):
                    raise
                logger.warning(
                    "Provider rejected response_format=json_object, "
                    "falling back to plain chat: %s",
                    exc,
                )
                self._json_mode = False

        raw = self.chat(system, user, **kwargs)
        return self._safe_json(raw)

    @staticmethod
    def _json_mode_unsupported(exc: Exception) -> bool:
        """True when the failure looks like "this provider has no json_object".

        Deliberately narrow: a transient network error must NOT be mistaken for
        a capability gap, otherwise we would silently downgrade for the rest of
        the process.
        """
        text = str(exc).lower()
        mentions_param = "response_format" in text or "json_object" in text
        mentions_rejection = any(
            word in text
            for word in (
                "unsupported",
                "not supported",
                "unrecognized",
                "unknown",
                "invalid",
                "unexpected",
            )
        )
        return mentions_param and mentions_rejection

    def _extract_json(self, system: str, user: str) -> dict:
        """Structured-extraction call, reasoning chain disabled.

        抽取是**确定性变换**（文本 → 固定 schema 的 JSON），模型的思考链
        在这里不产生任何价值，却把延迟从秒级推到分钟级（见 ``_NO_THINKING``）。
        因此抽取类调用一律走这里，匹配 / 评分这类需要判断的调用仍用
        :meth:`chat_json`，保留推理。

        非 Qwen 提供方不认识 ``enable_thinking``，会以 400 拒绝。这里沿用
        [chat_json] 的同一套「探测一次、记住结果」策略：首次被拒后就不再传，
        避免每次调用都付一次注定失败的 400。
        """
        if self._no_thinking is not False:
            try:
                result = self.chat_json(system, user, extra_body=_NO_THINKING)
                self._no_thinking = True
                return result
            except Exception as exc:
                if not self._thinking_unsupported(exc):
                    raise
                logger.warning(
                    "Provider rejected enable_thinking, falling back to "
                    "reasoning-enabled extraction: %s",
                    exc,
                )
                self._no_thinking = False

        return self.chat_json(system, user)

    @staticmethod
    def _thinking_unsupported(exc: Exception) -> bool:
        """True when the failure looks like "no such argument: enable_thinking".

        Kept narrow on purpose: a timeout or network error must NOT be read as
        a capability gap, or we would silently lose the speedup for the whole
        process.
        """
        text = str(exc).lower()
        return "enable_thinking" in text

    # ── Domain-specific helpers ───────────────────────────────────────────

    def parse_job(self, description: str) -> dict:
        """Extract structured fields from a job description."""
        return self.chat_json(JOB_PARSE_PROMPT, description)

    def parse_resume(self, resume_text: str) -> dict:
        """Extract structured skill graph from resume text.

        Runs through :meth:`_extract_json` (reasoning chain disabled) —
        see ``_NO_THINKING`` for the measured latency impact.
        """
        return self._extract_json(RESUME_PARSE_PROMPT, resume_text)

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

    def score_capabilities(self, resume_profile: dict, job_text: str) -> dict:
        """Careerly C4: score 4-6 capability dimensions from resume vs target JD.

        Returns ``{"capabilities": [{"name", "yours", "target", "why", "actions"}]}``.
        """
        user_prompt = (
            f"【简历画像】\n{json.dumps(resume_profile, ensure_ascii=False, indent=2)}\n\n"
            f"【目标岗位JD】\n{job_text}"
        )
        return self.chat_json(CAPABILITY_SCORING_PROMPT, user_prompt)

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
    def _strip_fence(text: str) -> str:
        """Drop a surrounding ``` / ```json markdown fence."""
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1]
            text = text.rsplit("```", 1)[0]
        return text.strip()

    @staticmethod
    def _escape_stray_quotes(text: str) -> str:
        """Escape unescaped double quotes sitting inside a JSON string value.

        The model copies quotes straight out of the source text, e.g.

            "highlights": ["晋级第五届“互联网+"大学生创新创业大赛"]

        The half-width quote after ``+`` closes the string early, so the rest
        becomes an illegal token and ``json.loads`` raises
        ``Expecting ',' delimiter`` — which used to collapse a whole resume
        into an empty profile.

        Telling a value's closing quote from a stray one inside it: a closing
        quote can only be followed by ``,`` ``}`` ``]`` ``:`` (or end of text),
        ignoring whitespace. Anything else means the quote was content, so we
        backslash it and stay inside the string.
        """
        out: list[str] = []
        in_string = False
        escaped = False
        index = 0
        length = len(text)

        while index < length:
            char = text[index]

            if not in_string:
                out.append(char)
                if char == '"':
                    in_string = True
                    escaped = False
                index += 1
                continue

            if escaped:  # previous char was a backslash: take this one verbatim
                out.append(char)
                escaped = False
            elif char == "\\":
                out.append(char)
                escaped = True
            elif char == '"':
                lookahead = index + 1
                while lookahead < length and text[lookahead] in " \t\r\n":
                    lookahead += 1
                following = text[lookahead] if lookahead < length else ""
                # `following == ""` at end of text is `in` the set, i.e. a closer.
                if following in ',}]:':
                    out.append(char)
                    in_string = False
                else:
                    out.append('\\"')
            else:
                out.append(char)
            index += 1

        return "".join(out)

    @staticmethod
    def _loads(text: str) -> Any:
        """Parse text as JSON, or None if it isn't JSON."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None

    @classmethod
    def _json_candidates(
        cls, text: str, open_char: str, close_char: str
    ) -> list[tuple[str, str]]:
        """Parse candidates ordered cheapest-first, each labelled with how it was derived.

        The order *is* the repair strategy: every candidate is only reached
        after the previous one failed, so the least destructive transformation
        that can possibly work is always tried first.
        """
        stripped = cls._strip_fence(text)
        start = stripped.find(open_char)
        end = stripped.rfind(close_char)
        sliced = (
            stripped[start : end + 1] if start != -1 and end != -1 else stripped
        )

        candidates = [(stripped, "raw")]
        if sliced != stripped:
            candidates.append((sliced, "sliced"))
        repaired = cls._escape_stray_quotes(sliced)
        if repaired != sliced:
            candidates.append((repaired, "escaped_stray_quotes"))
        return candidates

    @classmethod
    def _safe_json(cls, text: str) -> dict:
        """Extract and parse a JSON object from LLM output.

        Tolerates markdown fences, surrounding prose, and unescaped quotes
        inside string values.
        """
        for candidate, how in cls._json_candidates(text, "{", "}"):
            result = cls._loads(candidate)
            if isinstance(result, dict):
                if how == "escaped_stray_quotes":
                    logger.warning(
                        "LLM output had unescaped quotes inside string values; "
                        "repaired before parsing."
                    )
                return result
        # ⚠️ ERROR 级且默认 INFO 下必输出 —— 此前直接把模型原文前 300 字符
        # 写进日志，而那段文字来自简历 / JD（TODO K21）。
        # 换成结构骨架后，"为什么解析失败"的全部判据仍然可见。
        logger.error(
            "Failed to parse LLM output as JSON: %s", structural_preview(text, 300)
        )
        return {}

    @classmethod
    def _safe_json_array(cls, text: str) -> list[dict]:
        """Extract and parse a JSON array from LLM output."""
        for candidate, how in cls._json_candidates(text, "[", "]"):
            result = cls._loads(candidate)
            if isinstance(result, list):
                if how == "escaped_stray_quotes":
                    logger.warning(
                        "LLM output had unescaped quotes inside string values; "
                        "repaired before parsing."
                    )
                return result
            # Sometimes the model wraps the array in an object.
            if isinstance(result, dict):
                for key in ("results", "matches", "data", "items"):
                    if isinstance(result.get(key), list):
                        return result[key]
        # ⚠️ 同 `_safe_json_object`：ERROR 级，生产必输出（TODO K21）。
        logger.error(
            "Failed to parse LLM output as JSON array: %s", structural_preview(text, 300)
        )
        return []
