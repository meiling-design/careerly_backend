"""Pydantic models mirroring the Careerly Flutter JSON contract.

⚠️ WHY camelCase FIELD NAMES
---------------------------
Every field name below is copied **verbatim** from the corresponding Dart
model's ``toJson()`` / ``fromJson()`` (see
``G:/ai_agent/careerly/lib/data/models/*.dart``).  Renaming them to Python
style (``raw_jd``, ``created_at`` …) would silently break
``Model.fromJson`` on the client, so the camelCase names are kept on purpose
and are the single source of truth for the wire format.

Timestamps are emitted as ISO-8601 strings because the Dart side parses them
with ``DateTime.tryParse``; ``None`` is emitted as JSON ``null``.
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ══════════════════════════════════════════════════════════════════════════
#  Analysis — job / match / result / project
# ══════════════════════════════════════════════════════════════════════════


class JobModel(BaseModel):
    """`lib/data/models/job.dart` → ``Job``."""

    id: str
    title: str = ""
    company: str = ""
    rawJd: str = ""
    source: Optional[str] = None
    createdAt: Optional[str] = None


class StrengthScore(BaseModel):
    """`analysis_result.dart` → ``StrengthScore`` (Figma visual extension)."""

    name: str = ""
    score: int = 0


class GapDetail(BaseModel):
    """`analysis_result.dart` → ``GapDetail``.

    ⚠️ ``yours`` / ``target`` are the numeric gap inputs.  The current engine
    has **no per-capability 0–100 score** (unresolved conflict C4), so they
    are emitted as ``null`` and Dart's ``GapDetail.gap`` getter stays ``null``.
    """

    name: str = ""
    yours: Optional[int] = None
    target: Optional[int] = None
    level: Optional[str] = None
    why: Optional[str] = None
    actions: list[str] = Field(default_factory=list)


class JobMatch(BaseModel):
    """`analysis_result.dart` → ``JobMatch``.

    PRD §9.4 hard rule: ``score`` is 0–100 and derived **only** from
    capability matching — salary / commute / city must never feed into it.
    """

    jobId: str
    score: int = 0
    rank: int = 0
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    suggestion: Optional[str] = None
    strengthScores: list[StrengthScore] = Field(default_factory=list)
    gapDetails: list[GapDetail] = Field(default_factory=list)


class JobSnapshot(BaseModel):
    """一次分析实际使用的岗位原文快照（P1-02）。

    ⚠️ **不可变**：快照在分析成功时写入，之后修改岗位信息、
    重新解析链接都不会回写这里。``available=False`` 表示这是历史数据
    （功能上线前的分析记录）—— 前端必须展示「原文缺失」，
    **不得**回退到 ``jobs.raw_jd`` 冒充历史原文。
    """

    jobId: str
    title: str = ""
    company: str = ""
    rawJd: str = ""
    source: Optional[str] = None          # paste | url | history
    sourceUrl: Optional[str] = None
    importMethod: Optional[str] = None
    capturedAt: Optional[str] = None
    available: bool = True


class AnalysisResult(BaseModel):
    """`analysis_result.dart` → ``AnalysisResult`` (versioned, never overwritten)."""

    id: str
    projectId: str = ""
    version: int = 1
    resumeVersionId: Optional[str] = None
    resumeVersionLabel: Optional[str] = None
    matches: list[JobMatch] = Field(default_factory=list)
    capabilitySnapshot: dict[str, int] = Field(default_factory=dict)
    #: 本次分析实际使用的岗位原文快照（P1-02）。历史记录为空 /
    #: ``available=False``，前端据此显示缺失态，**不**读当前岗位。
    jobSnapshots: list[JobSnapshot] = Field(default_factory=list)
    createdAt: Optional[str] = None


class AnalysisProject(BaseModel):
    """`analysis_project.dart` → ``AnalysisProject``.

    Container: ``jobs`` (reusable ``Job`` entities) × ``results`` (versions).
    Removing a job from a project must NOT delete the ``Job`` entity.
    """

    id: str
    name: str = ""
    jobs: list[JobModel] = Field(default_factory=list)
    results: list[AnalysisResult] = Field(default_factory=list)
    createdAt: Optional[str] = None
    updatedAt: Optional[str] = None


class QuotaState(BaseModel):
    """`quota_state.dart` → ``QuotaState``.

    ``reserved`` 是「已被进行中的分析任务预占、尚未确认消耗」的次数
    （P1-03）。剩余额度 = ``total - used - reserved``。
    """

    total: int = 3
    used: int = 0
    reserved: int = 0
    isMember: bool = False


# ══════════════════════════════════════════════════════════════════════════
#  Analysis — request bodies (backend-only, not in the Dart contract)
# ══════════════════════════════════════════════════════════════════════════


class AnalyzeRequest(BaseModel):
    """`AnalysisRepository.analyze({required title, required rawJd})`.

    ``resumeText`` / ``company`` / ``source`` / ``sourceUrl`` 是
    **可选后端扩展**：Dart 契约里没有这些字段，但 P1-02 的 JD 快照
    需要记录「这次分析到底用了哪份岗位原文、从哪来」。
    缺省值保持旧行为（粘贴 JD、无公司名）。
    """

    title: str = Field(..., min_length=1, max_length=200)
    rawJd: str = Field(..., min_length=1)
    resumeText: Optional[str] = None
    company: str = ""
    #: 导入方式：``paste`` / ``url`` / ``history``。
    source: str = "paste"
    #: 岗位链接导入时的原始 URL。
    sourceUrl: Optional[str] = Field(default=None, max_length=2000)


class ParseJobUrlRequest(BaseModel):
    """`POST /api/v1/analysis/jobs/parse-url` 请求体（backend-only）。

    ⚠️ 与 [AnalyzeRequest] 一样**不在 Dart 契约文档里** ——
    它是 P4-③ 新增的能力，契约文档由另一侧维护，本文件不自作主张改它。
    """

    url: str = Field(..., min_length=1, max_length=2000)


class ParsedJob(BaseModel):
    """一条**从链接抓取得到的岗位**（`lib/data/models/parsed_job.dart`）。

    只承载「回填 JD 文本框」所需的最小字段：
    前端拿到它就把 ``description`` 填进 JD 输入框、``title`` 填进岗位名，
    用户**看得到、改得动**，之后仍走原来的「开始分析」链路。

    刻意**不返回** CrawlAgent 的全部字段（skills / location / department …）：
    匹配计算在后端自己会重新解析 JD，前端多拿这些只会诱使 UI 去展示
    尚未被产品确认的字段。
    """

    url: str
    title: str
    company: str = ""
    description: str


class AiHistoryMessage(BaseModel):
    """一条**已发生**的对话轮次，由客户端回传（backend-only）。

    会话历史存在**客户端本地**（裁决 2026-10-03 ②），后端不留存，
    因此多轮上下文靠请求体带回来。``role`` 只接受 ``user`` / ``assistant``
    —— 服务端在 ``LLMService.chat_stream`` 里会再过滤一次，这里限制
    长度是为了让超长请求在 FastAPI 校验层就被拒，而不是拖到 LLM 调用。
    """

    role: str = Field(..., pattern="^(user|assistant)$")
    content: str = Field(..., max_length=8000)


class AiMessageRequest(BaseModel):
    """`POST /api/v1/ai/messages` 请求体（backend-only，不在 Dart 契约文档里）。

    ⚠️ **不带 user id** —— 用户身份一律取自 JWT（``get_current_user``），
    客户端传什么都不影响数据作用域（``CareerlyStore.get_project`` 在 SQL
    里按 ``user_id`` 过滤）。
    """

    message: str = Field(..., min_length=1, max_length=4000)

    #: 入口带参：从岗位详情 / 分析结果进入时携带。缺省则回退最近一次分析。
    projectId: Optional[str] = None

    #: 入口带参：聚焦到某一个岗位的匹配结果（可选）。
    jobId: Optional[str] = None

    #: 本次会话此前的轮次（客户端本地存储，随请求回传）。
    history: list[AiHistoryMessage] = Field(default_factory=list)


# ══════════════════════════════════════════════════════════════════════════
#  Resume — versions / profile
# ══════════════════════════════════════════════════════════════════════════


class ResumeVersion(BaseModel):
    """`resume_profile.dart` → ``ResumeVersion``."""

    id: str
    label: str = ""
    fileName: Optional[str] = None
    uploadedAt: Optional[str] = None
    isCurrent: bool = False


class ResumeProfile(BaseModel):
    """`resume_profile.dart` → ``ResumeProfile``."""

    versions: list[ResumeVersion] = Field(default_factory=list)
    workExperiences: list[str] = Field(default_factory=list)
    projectExperiences: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    awards: list[str] = Field(default_factory=list)
    capabilities: dict[str, int] = Field(default_factory=dict)
    updatedAt: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════
#  Growth — events / capabilities
# ══════════════════════════════════════════════════════════════════════════


class GrowthEvent(BaseModel):
    """`growth_event.dart` → ``GrowthEvent``.

    ⚠️ ``type`` MUST be the Dart enum's ``.name`` — one of
    ``experience | skill | capability | matchScore | rank``.
    The Chinese labels (新增经历 / 技能变化 / …) are display-only and would
    fail ``GrowthEventType.tryParse``.
    """

    id: str
    type: str = "experience"
    title: str = ""
    description: Optional[str] = None
    delta: Optional[int] = None
    occurredAt: Optional[str] = None


class Capability(BaseModel):
    """`capability.dart` → ``Capability`` (distinct from ``SkillGroup.skills``)."""

    name: str = ""
    score: int = 0
    delta: int = 0
    previousScore: Optional[int] = None


# ══════════════════════════════════════════════════════════════════════════
#  Career assets — the rich "我的" aggregate
# ══════════════════════════════════════════════════════════════════════════


class SkillDelta(BaseModel):
    """`work_experience.dart` → ``SkillDelta`` (inner part of WorkExperience)."""

    name: str = ""
    delta: int = 0


class WorkExperience(BaseModel):
    """`work_experience.dart` → ``WorkExperience``."""

    id: str
    period: str = ""
    title: str = ""
    company: str = ""
    description: str = ""
    skills: list[SkillDelta] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)


class JobImpact(BaseModel):
    """`project_experience.dart` → ``JobImpact``."""

    job: str = ""
    delta: int = 0


class ProjectExperience(BaseModel):
    """`project_experience.dart` → ``ProjectExperience``."""

    id: str
    name: str = ""
    year: str = ""
    type: str = ""
    role: str = ""
    description: str = ""
    outcome: str = ""
    skills: list[str] = Field(default_factory=list)
    jobImpact: list[JobImpact] = Field(default_factory=list)


class SkillGroup(BaseModel):
    """`career_skill.dart` → ``SkillGroup``."""

    category: str = ""
    skills: list[str] = Field(default_factory=list)


class Award(BaseModel):
    """`award.dart` → ``Award``."""

    name: str = ""
    year: str = ""
    org: str = ""
    description: str = ""


class CareerAssets(BaseModel):
    """`career_assets.dart` → ``CareerAssets``.

    ⚠️ Counts (workCount / projectCount / skillCount …) are **derived by the
    app** — the backend must never send count fields.
    """

    workExperiences: list[WorkExperience] = Field(default_factory=list)
    projects: list[ProjectExperience] = Field(default_factory=list)
    skillGroups: list[SkillGroup] = Field(default_factory=list)
    awards: list[Award] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)
    spanLabel: str = ""
    monthlyAdded: int = 0


# ══════════════════════════════════════════════════════════════════════════
#  Profile / home
# ══════════════════════════════════════════════════════════════════════════


class UserProfile(BaseModel):
    """`user_profile.dart` → ``UserProfile``."""

    displayName: str = ""
    handle: str = ""
    membershipLabel: str = ""


class MatchChange(BaseModel):
    """`home_summary.dart` → ``MatchChange``."""

    jobId: str
    jobTitle: str = ""
    company: Optional[str] = None
    score: int = 0
    delta: Optional[int] = None


class HomeSummary(BaseModel):
    """`home_summary.dart` → ``HomeSummary``."""

    headline: str = ""
    subheadline: str = ""
    suggestions: list[str] = Field(default_factory=list)
    matchChanges: list[MatchChange] = Field(default_factory=list)
    recentGrowth: list[str] = Field(default_factory=list)
    dailyContent: Optional[str] = None
    hasBaseline: bool = False


# ── helpers ───────────────────────────────────────────────────────────────


def iso(ts: float | None) -> Optional[str]:
    """Unix seconds -> ISO-8601 string that ``DateTime.tryParse`` accepts."""
    if ts is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def dump(model: BaseModel) -> dict[str, Any]:
    """``model_dump`` with ``None`` preserved (Dart relies on null checks)."""
    return model.model_dump(mode="json")
