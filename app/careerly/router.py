"""Careerly API endpoints — the contract-aligned surface for the Flutter app.

Wire contract: ``docs/careerly/BACKEND_API_CONTRACT.md`` (§1/§2).  Every JSON
shape is dictated by the Dart ``fromJson``/``toJson`` — do not rename fields.

Endpoints (all require a JWT via ``get_current_user``):

    POST /api/v1/analysis                    analyze({title, rawJd}) — synchronous
    POST /api/v1/analysis/tasks              same, but 202 + taskId (P1-1b 异步)
    GET  /api/v1/analysis/projects           list analysis projects
    GET  /api/v1/analysis/projects/{pid}     one project (jobs + versioned results)
    POST /api/v1/analysis/jobs/parse-url     crawl a job URL → backfill the JD box (③)
    GET  /api/v1/analysis/tasks              list own analysis tasks (P1-1a 可观测)
    GET  /api/v1/analysis/tasks/{tid}        one task + real node progress (P1-1b)
    POST /api/v1/resume                      submit a resume asset (file/image/text)
                                             → parse → **append a new version**
                                             → make it the current baseline
    PUT  /api/v1/resume                      save resume baseline text (P3 preview)
    GET  /api/v1/resume                      current resume + all versions
    PUT  /api/v1/resume/versions/{vid}/current   promote a version (PRD §14)
    DELETE /api/v1/resume/versions/{vid}         delete an archived version
    GET  /api/v1/quota                       QuotaState (Baseline §5: free 3)
    POST /api/v1/ai/messages                 AI advisor — SSE streaming (P-AI-1)
    GET  /api/v1/growth/events               growth footprint events (P2-4)
    GET  /api/v1/growth/timeline             events within an occurredAt range (P2-4)
    GET  /api/v1/career/assets               CareerAssets — 「我的」职业资产 (P3-1)
    GET  /api/v1/home/summary                HomeSummary — 首页摘要 (P3-2)
    GET  /api/v1/profile                     UserProfile — 「我的」身份资料 (P3-3)

Analysis is synchronous (decision C2): the engine call is offloaded to a
thread executor so the event loop stays responsive during the 10–20s run.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import time
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.agents.resume_agent import ResumeAgent, ResumeExtractionError
from app.api.envelope import err, ok
from app.core.security import get_current_user
from app.core.state_store import StateStore
from app.careerly import assets as career_assets
from app.careerly import growth, home, identity
from app.careerly.ai_context import (
    SYSTEM_PROMPT,
    build_current_context,
    build_user_prompt,
)
from app.careerly.engine import AnalysisCancelled, AnalysisError, run_analysis
from app.careerly.job_import import JobImportError, fetch_job_from_url
from app.careerly.schemas import (
    AiMessageRequest,
    AnalyzeRequest,
    JobSnapshot,
    ParseJobUrlRequest,
    dump,
    iso,
)
from app.careerly.store import CareerlyStore
from app.services.llm_service import LLMService
from app.utils.logger import logger

router = APIRouter(prefix="/api/v1", tags=["careerly"])

_store: CareerlyStore | None = None


def get_store() -> CareerlyStore:
    """Lazy singleton; tests may replace ``router._store``."""
    global _store
    if _store is None:
        _store = CareerlyStore()
    return _store


def recover_interrupted_tasks() -> int:
    """应用启动时清理遗留的 ``queued`` / ``processing`` 任务（P1-06）。

    由 ``app/api/server.py`` 的 startup 钩子调用。**不重新执行**分析 ——
    协程无法跨进程恢复，重跑没有可靠依据，而且可能重复扣费。
    只保证状态正确：``failed`` + ``TASK_INTERRUPTED`` + 释放额度预占。
    """
    try:
        recovered = get_store().recover_interrupted_tasks()
        logger.info("Careerly startup recovery | interrupted=%d", recovered)
        return recovered
    except Exception:  # pragma: no cover - startup must not crash the app
        logger.exception("Careerly startup recovery failed")
        return 0


_engine_store: StateStore | None = None


def get_engine_store() -> StateStore:
    """惰性单例 —— 读引擎 session 的实时进度用（P1-1b）。

    ⚠️ 这是**另一个数据库**（``graph_state.db``）。Careerly 的业务数据
    （``careerly.db``）与引擎轨迹刻意分库，这里只做**只读**的进度查询，
    不把引擎状态并进业务表。
    """
    global _engine_store
    if _engine_store is None:
        _engine_store = StateStore()
    return _engine_store


# ── 分析进度：引擎节点 → 产品步骤（P1-1b）───────────────────────────────────
#
# 引擎跑 8 个 LangGraph 节点，「正在分析」页只展示 5 步（文案逐字取自 Figma）。
# 两者**不是一一对应** —— 5 步是 8 节点的产品级抽象。这一步映射只应该有一处
# 定义：放在契约层（本文件），Flutter 就不需要认识引擎的节点名。
#
# | UI 步骤（Figma 原型） | 引擎节点 |
# |---|---|
# | 0 岗位需求解析 | ``crawl`` + ``normalize`` |
# | 1 简历信息解析 | ``resume_embed`` |
# | 2 能力匹配分析 | ``routing`` + ``match``（含 ``recrawl`` 回环） |
# | 3 识别能力 Gap | ``rank`` |
# | 4 生成分析报告 | ``report`` |
ANALYSIS_STEP_COUNT = 5

_NODE_TO_STEP: dict[str, int] = {
    "crawl": 0,
    "normalize": 0,
    "resume_embed": 1,
    "routing": 2,
    "match": 2,
    "recrawl": 2,
    "rank": 3,
    "report": 4,
}

#: 建图时登记的节点总数（``orchestrator.DecisionGraph``）。
#: ⚠️ 与 UI 的 5 步**不是同一个刻度**，故响应里分成两个字段各自报告，
#: 不要拿它去除以 ``totalSteps``。
ENGINE_NODE_COUNT = 8

#: 每个产品步骤「完成」所依据的引擎节点。
#: ``recrawl`` 是 step 2 的内部回环，不参与完成判定 —— 否则一次回环会把
#: 已经完成的步骤重新算成未完成。
STEP_NODES: tuple[tuple[str, ...], ...] = (
    ("crawl", "normalize"),
    ("resume_embed",),
    ("routing", "match"),
    ("rank",),
    ("report",),
)

#: 阶段权重。与 Figma ``analysisBeats.endPercent`` 的相邻差值一致：
#: ``[20, 40, 75, 90, 100]`` → ``[20, 20, 35, 15, 10]``。
#: 总进度只在这里加权，客户端不自行猜权重。
STEP_WEIGHTS: tuple[int, ...] = (20, 20, 35, 15, 10)

STEP_STATUS_COMPLETED = "completed"
STEP_STATUS_RUNNING = "running"
STEP_STATUS_PENDING = "pending"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_CANCELLED = "cancelled"


def _build_steps(
    *,
    task_status: str,
    node: str | None,
    node_status: str | None,
    completed_nodes: set[str],
    error_message: str | None,
) -> list[dict[str, Any]]:
    """把引擎节点状态翻译成 5 个产品步骤的**显式状态**。

    状态只从真实节点推导，不做任何时间估算：

    - ``completed_nodes`` 覆盖了某步的全部节点 → completed
    - 当前节点属于某步且尚未完成 → running
    - 任务 failed/cancelled → 当前步标红，其后 pending
    - 其余 → pending
    """
    statuses: list[str] = [STEP_STATUS_PENDING] * ANALYSIS_STEP_COUNT

    if task_status == CareerlyStore.TASK_COMPLETED:
        statuses = [STEP_STATUS_COMPLETED] * ANALYSIS_STEP_COUNT
    elif task_status in (
        CareerlyStore.TASK_FAILED,
        CareerlyStore.TASK_CANCELLED,
    ):
        failed_step = _NODE_TO_STEP.get(node) if isinstance(node, str) else None
        if failed_step is not None:
            for i in range(ANALYSIS_STEP_COUNT):
                if i < failed_step:
                    statuses[i] = STEP_STATUS_COMPLETED
                elif i == failed_step:
                    statuses[i] = (
                        STEP_STATUS_CANCELLED
                        if task_status == CareerlyStore.TASK_CANCELLED
                        else STEP_STATUS_FAILED
                    )
    else:
        for i, nodes in enumerate(STEP_NODES):
            if nodes and all(n in completed_nodes for n in nodes):
                statuses[i] = STEP_STATUS_COMPLETED
            elif isinstance(node, str) and node in nodes:
                statuses[i] = (
                    STEP_STATUS_FAILED
                    if node_status == "failed"
                    else STEP_STATUS_RUNNING
                )
        # 失败若发生在尚未落完成集合的节点上，仍按「当前节点所在步」标红。
        if node_status == "failed" and isinstance(node, str):
            failed_step = _NODE_TO_STEP.get(node)
            if failed_step is not None:
                for i in range(ANALYSIS_STEP_COUNT):
                    if i < failed_step:
                        statuses[i] = STEP_STATUS_COMPLETED
                    elif i == failed_step:
                        statuses[i] = STEP_STATUS_FAILED

    steps: list[dict[str, Any]] = []
    for i, status in enumerate(statuses):
        expected = STEP_NODES[i]
        done = sum(1 for n in expected if n in completed_nodes)

        # 步内百分比只在**能真实算出来**时给出：该步有多个节点且已完成
        # 其中一部分。单节点步骤（LLM 生成中）无法精确预估 → ``None``，
        # 客户端展示不确定态动画，**不允许**用本地时钟补一个假数字。
        progress_percent: int | None = None
        if status == STEP_STATUS_COMPLETED:
            progress_percent = 100
        elif status == STEP_STATUS_RUNNING and len(expected) > 1:
            if 0 < done < len(expected):
                progress_percent = round(done / len(expected) * 100)

        steps.append({
            "index": i,
            "status": status,
            "progressPercent": progress_percent,
            "error": (
                error_message
                if status in (STEP_STATUS_FAILED, STEP_STATUS_CANCELLED)
                else None
            ),
        })
    return steps


def _overall_progress(steps: list[dict[str, Any]], task_status: str) -> int:
    """按阶段权重汇总总进度。

    - 只有**全部 5 步 completed** 且任务 completed 才允许 100%
    - 其余情况封顶 99%，保证「完成前永不到 100」
    - 进行中的步若有真实步内百分比，按权重线性计入（不是匀速自增）
    """
    if task_status == CareerlyStore.TASK_COMPLETED and all(
        s["status"] == STEP_STATUS_COMPLETED for s in steps
    ):
        return 100

    total = 0.0
    for step in steps:
        weight = STEP_WEIGHTS[step["index"]]
        if step["status"] == STEP_STATUS_COMPLETED:
            total += weight
        elif (
            step["status"] == STEP_STATUS_RUNNING
            and step["progressPercent"] is not None
        ):
            total += weight * (step["progressPercent"] / 100.0)
    return min(99, int(round(total)))


def _progress_payload(task: dict[str, Any] | str | None) -> dict[str, Any] | None:
    """把引擎 session 的实时进度翻译成任务响应里的 ``progress``。

    读不到（没有 session / 尚未开始 / 引擎未落进度）时返回 ``None`` ——
    客户端据此显示「排队中」，而**不是**伪造一个 0% 进度。

    ``steps`` 是本次改造的核心：5 个节点各自的真实状态由**后端**判定，
    客户端只负责渲染，不再用「当前步号 + 本地时钟」推断。

    兼容旧调用：传入裸 ``session_id`` 字符串时按「processing、无任务行」
    处理（仅供进度映射的单测使用；HTTP 路径一律传任务 dict）。
    """
    if task is None:
        return None
    if isinstance(task, str):
        task = {"sessionId": task, "status": CareerlyStore.TASK_PROCESSING}
    session_id = task.get("sessionId")
    if not session_id:
        return None
    raw = get_engine_store().get_session_progress(session_id)
    if not raw:
        return None

    node = raw.get("current_node")
    node = node if isinstance(node, str) else None
    completed_nodes = raw.get("completed_nodes")
    if not isinstance(completed_nodes, list):
        completed_nodes = []
    completed_set = {n for n in completed_nodes if isinstance(n, str)}

    steps = _build_steps(
        task_status=task.get("status") or CareerlyStore.TASK_QUEUED,
        node=node,
        node_status=raw.get("node_status"),
        completed_nodes=completed_set,
        error_message=task.get("errorMessage"),
    )
    overall = _overall_progress(steps, task.get("status") or "")

    return {
        "currentNode": node,
        "nodeStatus": raw.get("node_status"),
        "completedCount": raw.get("completed_count"),
        "totalNodes": ENGINE_NODE_COUNT,
        # 产品步骤（0 基）。未知节点 → None，客户端保持当前步不动。
        "stepIndex": _NODE_TO_STEP.get(node) if node else None,
        "totalSteps": ANALYSIS_STEP_COUNT,
        "updatedAt": raw.get("_updated_at"),
        # 新增：5 步真实状态 + 后端加权总进度。旧字段保留以兼容旧客户端。
        "steps": steps,
        "overallProgress": overall,
    }


# ── Analysis ────────────────────────────────────────────────────────────────


class _PreparedAnalysis:
    """``_prepare_task`` 的返回值：要么带 ``failure``，要么带完整运行参数。"""

    def __init__(
        self,
        *,
        failure: Any = None,
        task_id: str | None = None,
        session_id: str | None = None,
        resume_text: str | None = None,
        resume_version_id: str | None = None,
        resume_version_label: str | None = None,
        jd_snapshot: dict[str, Any] | None = None,
    ):
        self.failure = failure
        self.task_id = task_id
        self.session_id = session_id
        self.resume_text = resume_text
        self.resume_version_id = resume_version_id
        self.resume_version_label = resume_version_label
        self.jd_snapshot = jd_snapshot


def _prepare_task(
    store: CareerlyStore,
    user_id: int,
    req: AnalyzeRequest,
) -> _PreparedAnalysis:
    """建任务 + 预占额度 + 跑前置校验。

    ── P1-1a：任务留痕，**在一切校验之前**建任务。
    为什么必须在最前面：配额不足、缺少基线这类前置拒绝恰恰是最常见的失败，
    若等校验通过才建任务，它们就一个记录都不会留下 —— 「失败无痕」正是
    P1-2 要消灭的东西。前置拒绝同样记 failed（started_at 保持 NULL，
    表示引擎从未真正启动）。

    ── P1-1b：session id 在这里**先于引擎**生成。顺序是刻意的：
    先生成 id → 写进任务行 → 交给引擎，运行期间 ``GET /analysis/tasks/{id}``
    才有东西可查。反过来（等引擎自己生成再回写）在同步接口下永远拿不到。

    ── P1-01 / P1-02：在任务创建时把「实际使用的简历版本」和
    「实际使用的 JD 原文」固定下来。之后用户切换简历 / 修改岗位，
    都不会改写这次分析已经绑定的证据。

    ── P1-03：额度从「先检查后扣减」改为**原子预占**。
    预占失败（免费额度耗尽）才拒绝；成功 / 失败分别由任务终态
    确认消耗或释放。同步与异步入口共用这一段，因此两条路径的额度
    语义逐字一致。
    """
    session_id = StateStore.new_session_id()
    captured_at = iso(time.time())
    jd_snapshot = {
        "title": req.title.strip(),
        "company": (req.company or "").strip(),
        "rawJd": req.rawJd,
        "source": req.source or "paste",
        "sourceUrl": req.sourceUrl,
        "importMethod": req.source or "paste",
        "capturedAt": captured_at,
    }

    # 简历来源：内联文本优先；否则用当前简历版本（同时拿到版本 id）。
    # 内联文本没有版本 id —— 它本来就不是「用户简历库里的某一版」，
    # 不能伪造一个绑定。
    resume_text = (req.resumeText or "").strip()
    resume_version_id: str | None = None
    resume_version_label: str | None = None
    current = store.get_current_resume_version(user_id)
    if not resume_text:
        if current is not None:
            resume_version_id = current.get("id")
            resume_version_label = current.get("label")
            resume_text = (current.get("text") or "").strip()
    elif current is not None and (current.get("text") or "").strip() == resume_text:
        # 内联文本与当前版本逐字相同 → 它就是那一版，可以安全绑定。
        # 不同则视为 ad-hoc 文本，**不**绑定、也**不**回写当前简历。
        resume_version_id = current.get("id")
        resume_version_label = current.get("label")

    task_id = store.create_task(
        user_id,
        req.title,
        source=req.source or "paste",
        session_id=session_id,
        resume_version_id=resume_version_id,
        resume_version_label=resume_version_label,
        jd_snapshot=jd_snapshot,
    )

    # Quota gate (Baseline §5 / PRD §16): free users get 3 analyses total.
    # ⚠️ 这里必须是**原子预占**，不能先 get_quota 再在成功后 consume ——
    # 那样两个并发请求会同时通过检查（P1-03）。
    reserved = store.reserve_analysis_quota(user_id, task_id)
    if reserved == CareerlyStore.QUOTA_EXHAUSTED:
        store.fail_task(task_id, "QUOTA_EXCEEDED", "免费分析次数已用完")
        quota = store.get_quota(user_id)
        return _PreparedAnalysis(failure=err(
            "QUOTA_EXCEEDED",
            "免费分析次数已用完，升级会员后可继续分析",
            403,
            details={"quota": dump(quota)},
        ))

    if not resume_text:
        # P1-2：与 engine.run_analysis 的 NO_BASELINE 保持同一错误码，
        # 否则「内联简历缺失」和「基线缺失」会给出两个不同的 code，前端无法统一处理。
        message = "尚未建立职业基线：请先上传简历或传入 resumeText"
        store.fail_task(task_id, "NO_BASELINE", message)  # fail_task 会释放预占
        return _PreparedAnalysis(failure=err("NO_BASELINE", message, 422))

    return _PreparedAnalysis(
        task_id=task_id,
        session_id=session_id,
        resume_text=resume_text,
        resume_version_id=resume_version_id,
        resume_version_label=resume_version_label,
        jd_snapshot=jd_snapshot,
    )


async def _execute_analysis(
    *,
    store: CareerlyStore,
    user_id: int,
    task_id: str,
    session_id: str,
    title: str,
    raw_jd: str,
    resume_text: str,
    resume_version_id: str | None = None,
    resume_version_label: str | None = None,
    jd_snapshot: dict[str, Any] | None = None,
    cancel_check: Any = None,
):
    """跑引擎并把成功产物落库；返回 ``AnalysisProject``。

    失败一律抛 [AnalysisError]（含把意外崩溃归一化），由调用方决定是
    返回 HTTP 错误还是把任务标记为 failed。

    ⚠️ 同步 ``POST /analysis`` 与异步 ``POST /analysis/tasks`` **共用这一段**。
    两条入口只有「要不要等」不同；校验、引擎调用、落库必须逐字一致，
    否则同一份 JD 走不同入口会得到不同的库状态。
    """
    loop = asyncio.get_running_loop()
    try:
        project, resume_profile = await loop.run_in_executor(
            None,
            partial(
                run_analysis,
                title=title,
                raw_jd=raw_jd,
                resume_text=resume_text,
                session_id=session_id,
                cancel_check=cancel_check,
            ),
        )
    except AnalysisCancelled:
        raise
    except AnalysisError:
        raise
    except Exception as exc:  # pragma: no cover - unexpected engine crash
        logger.exception("Careerly analyze crashed | user=%d | task=%s", user_id, task_id)
        raise AnalysisError("ANALYSIS_FAILED", f"分析失败：{exc}", 502) from exc

    # 最后一道闸：取消后**绝不**写项目、绝不扣额度、绝不标完成。
    if cancel_check is not None and cancel_check():
        raise AnalysisCancelled()

    # P1-01 / P1-02：把「本次实际使用的简历版本 + JD 原文」绑定到
    # 结果记录上。这些值来自任务创建时的快照，不会再被后续修改影响。
    for result in project.results:
        result.resumeVersionId = resume_version_id
        result.resumeVersionLabel = resume_version_label
        if jd_snapshot:
            result.jobSnapshots = [
                JobSnapshot(
                    jobId=job.id,
                    title=jd_snapshot.get("title", job.title),
                    company=jd_snapshot.get("company", job.company),
                    rawJd=jd_snapshot.get("rawJd", raw_jd),
                    source=jd_snapshot.get("source"),
                    sourceUrl=jd_snapshot.get("sourceUrl"),
                    importMethod=jd_snapshot.get("importMethod"),
                    capturedAt=jd_snapshot.get("capturedAt"),
                    available=True,
                )
                for job in project.jobs
            ]

    store.save_project(user_id, project)
    # P3-1：把引擎解析出的简历画像落库 —— 这是「我的 → 职业资产」的真实数据源。
    # 在此之前 careerly_resumes.profile 永远是 {}，职业资产页只能走 Mock。
    # ⚠️ 只在画像非空时写：分析链路可能降级产出 {}，用空值覆盖已有画像
    # 会让「我的」在功能上回到 Mock 之前的状态。
    if resume_profile:
        if resume_version_id:
            # P1-01：写回**本次实际使用的那一版**，不碰分析期间可能已经
            # 切成当前的其他版本。
            store.update_resume_version_profile(
                user_id, resume_version_id, resume_profile
            )
        elif store.get_resume(user_id) is None:
            # 内联简历 + 用户还没有任何基线 → 这是唯一一份简历，
            # 建立基线是合理的最小行为（否则职业资产永远为空）。
            # ⚠️ 已有基线时不写：不能拿一次 ad-hoc 分析覆盖用户资产。
            store.save_resume(user_id, resume_text, resume_profile)
    growth.record_analysis_growth(  # P2/P1-03：快照 + 兼容性校验后的事件
        store, user_id, project,
        resume_text=resume_text,
        resume_version_id=resume_version_id,
        resume_version_label=resume_version_label,
    )
    # P1-03：额度确认与任务完成放在同一个条件更新里 ——
    # 任务已被取消 / 已完成时不会再扣一次。
    if not store.complete_task(task_id, project.id):
        logger.info(
            "Careerly analysis finished after task became terminal | task=%s",
            task_id,
        )
    return project


#: 后台分析任务的强引用。不保存引用的话，asyncio 可能在任务跑完前
#: 把它回收掉（事件循环只持弱引用）。
_background_tasks: set[asyncio.Task] = set()

#: 每个进行中任务的取消标志（task_id → Event）。
#:
#: 为什么用 ``threading.Event`` 而不是 ``asyncio.Task.cancel()``：
#: 引擎跑在 ``run_in_executor`` 的**线程**里，协程取消无法穿透线程。
#: 一个线程安全的布尔标志由引擎在每个节点边界主动检查，是唯一可靠的
#: 协作式中止点（见 ``orchestrator._update_status``）。
_task_cancel_events: dict[str, threading.Event] = {}


@router.post("/analysis")
async def analyze(
    req: AnalyzeRequest,
    current_user: dict = Depends(get_current_user),
):
    """Run one Gap analysis for a pasted JD and return the full project.

    **同步**（决策 C2 / D-A ② 第一阶段）：等引擎跑完 8 节点才返回，
    Flutter 零改动。需要看真实节点进度的客户端走 ``POST /analysis/tasks``。
    """
    store = get_store()
    user_id = int(current_user["id"])

    prepared = _prepare_task(store, user_id, req)
    if prepared.failure is not None:
        return prepared.failure
    task_id = prepared.task_id
    session_id = prepared.session_id

    if not store.start_task(task_id):
        # 任务在创建与启动之间被取消（或已到终态）：绝不继续跑引擎。
        return err("ANALYSIS_CANCELLED", "分析已取消", 409)
    try:
        project = await _execute_analysis(
            store=store,
            user_id=user_id,
            task_id=task_id,
            session_id=session_id,
            title=req.title,
            raw_jd=req.rawJd,
            resume_text=prepared.resume_text or "",
            resume_version_id=prepared.resume_version_id,
            resume_version_label=prepared.resume_version_label,
            jd_snapshot=prepared.jd_snapshot,
        )
    except AnalysisError as exc:
        # P1-2：失败必须落库，消灭「失败无痕」。
        store.fail_task(task_id, exc.code, exc.message)
        logger.warning("Careerly analyze failed | user=%d | task=%s | code=%s",
                       user_id, task_id, exc.code)
        return err(exc.code, exc.message, exc.status_code)
    except Exception as exc:  # pragma: no cover - 落库阶段的意外
        # P1-03 / P1-06：即使失败发生在落库阶段，也必须释放预占额度，
        # 否则任务会永久占着一次免费额度直到服务重启。
        store.fail_task(task_id, "ANALYSIS_FAILED", str(exc))
        logger.exception("Careerly analyze crashed | user=%d | task=%s", user_id, task_id)
        return err("ANALYSIS_FAILED", f"分析失败：{exc}", 502)

    logger.info("Careerly analyze | user=%d | project=%s | task=%s",
                user_id, project.id, task_id)
    return ok(dump(project), status_code=201)


@router.post("/analysis/tasks")
async def start_analysis_task(
    req: AnalyzeRequest,
    current_user: dict = Depends(get_current_user),
):
    """**异步**入口（P1-1b）：立刻返回 202 + ``taskId``，分析在后台跑。

    与同步 ``POST /analysis`` **并存**，不是替换 —— 见 D-A ②「双模兼容」。
    客户端拿到 ``taskId`` 后轮询 ``GET /analysis/tasks/{taskId}``，
    即可用真实节点推进「正在分析」页的 5 步动效。

    额度语义不变（Baseline §5）：仍然**成功才扣**，失败/前置拒绝不扣。
    """
    store = get_store()
    user_id = int(current_user["id"])

    prepared = _prepare_task(store, user_id, req)
    if prepared.failure is not None:
        return prepared.failure
    task_id = prepared.task_id
    session_id = prepared.session_id

    if not store.start_task(task_id):
        current = store.get_task(user_id, task_id)
        return ok(
            {
                "taskId": task_id,
                "status": (current or {}).get("status", CareerlyStore.TASK_CANCELLED),
            },
            status_code=202,
        )

    cancel_event = threading.Event()
    _task_cancel_events[task_id] = cancel_event

    async def _background() -> None:
        try:
            project = await _execute_analysis(
                store=store,
                user_id=user_id,
                task_id=task_id,
                session_id=session_id,
                title=req.title,
                raw_jd=req.rawJd,
                resume_text=prepared.resume_text or "",
                resume_version_id=prepared.resume_version_id,
                resume_version_label=prepared.resume_version_label,
                jd_snapshot=prepared.jd_snapshot,
                cancel_check=cancel_event.is_set,
            )
        except AnalysisCancelled:
            # 取消是正常路径：终态已由 cancel 接口写入，这里不写失败原因。
            store.cancel_task(task_id)
            logger.info("Careerly async analyze cancelled | user=%d | task=%s",
                        user_id, task_id)
            return
        except AnalysisError as exc:
            # 取消与异常竞态：用户已取消时，不要把 cancelled 覆盖成 failed。
            if cancel_event.is_set():
                store.cancel_task(task_id)
                return
            store.fail_task(task_id, exc.code, exc.message)
            logger.warning("Careerly async analyze failed | user=%d | task=%s | code=%s",
                           user_id, task_id, exc.code)
            return
        except Exception as exc:  # pragma: no cover - 落库阶段的意外
            if cancel_event.is_set():
                store.cancel_task(task_id)
                return
            store.fail_task(task_id, "ANALYSIS_FAILED", str(exc))
            logger.exception("Careerly async analyze crashed | user=%d | task=%s",
                             user_id, task_id)
            return
        finally:
            _task_cancel_events.pop(task_id, None)
        logger.info("Careerly async analyze | user=%d | project=%s | task=%s",
                    user_id, project.id, task_id)

    task = asyncio.create_task(_background())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)

    return ok(
        {"taskId": task_id, "status": CareerlyStore.TASK_PROCESSING},
        status_code=202,
    )


@router.get("/analysis/projects")
async def list_projects(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    store = get_store()
    projects = store.list_projects(int(current_user["id"]), limit=min(max(limit, 1), 200))
    return ok([dump(p) for p in projects])


@router.get("/analysis/jobs")
async def list_job_library(
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
):
    """历史岗位库（PRD §9.3）：从本项目移除岗位不删除岗位记录。"""
    store = get_store()
    jobs = store.list_jobs(int(current_user["id"]), limit=min(max(limit, 1), 200))
    return ok([dump(j) for j in jobs])


@router.post("/analysis/jobs/parse-url")
async def parse_job_url(
    req: ParseJobUrlRequest,
    current_user: dict = Depends(get_current_user),
):
    """③ 岗位 URL：抓取链接 → 回填 JD 文本框。

    **不是**分析：不扣额度、不建分析任务、不写任何库
    （Baseline §5 / CLAUDE.md §5.2 —— 只有「开始分析」扣额度）。
    细节与「为什么不让 /analysis 收 URL」见 ``app/careerly/job_import.py``。
    """
    user_id = int(current_user["id"])
    try:
        parsed = await fetch_job_from_url(req.url)
    except JobImportError as exc:
        logger.warning(
            "Careerly parse-job-url failed | user=%d | code=%s | url=%s",
            user_id, exc.code, req.url[:120],
        )
        return err(exc.code, exc.message, exc.status_code)
    except Exception as exc:  # pragma: no cover - unexpected crawler crash
        logger.exception("Careerly parse-job-url crashed | user=%d", user_id)
        return err("JOB_FETCH_FAILED", f"岗位链接抓取失败：{exc}", 502)

    return ok(dump(parsed))


@router.get("/analysis/projects/{project_id}")
async def get_project(
    project_id: str,
    current_user: dict = Depends(get_current_user),
):
    store = get_store()
    project = store.get_project(int(current_user["id"]), project_id)
    if project is None:
        return err("PROJECT_NOT_FOUND", f"分析项目 {project_id} 不存在", 404)
    return ok(dump(project))


# ── Analysis tasks（P1-1a 可观测面 / P1-1b 进度面）─────────────────────────
#
# 全部按 user 作用域。P1-1a 只读终态；P1-1b 起单任务读**实时节点进度**。
#
# ⚠️ 归属纪律：进度是从**引擎库**按 ``sessionId`` 读的，而 ``sessionId``
# 来自任务行。所以必须**先**用 ``store.get_task(user_id, task_id)`` 确认
# 该任务属于当前用户，**再**拿它携带的 sessionId 去查进度 —— 绝不能让
# 客户端直接传 session_id 进来查。否则就是一条绕过任务归属的越权读路径。


@router.get("/analysis/tasks")
async def list_tasks(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """本人最近的分析任务（含失败原因与耗时）。

    ⚠️ **不**在这里附带 ``progress``：列表是终态历史，逐条去引擎库查进度
    会产生 N+1 次查询，而列表页也不需要实时进度。要看进度请查单任务。
    """
    store = get_store()
    return ok(store.list_tasks(int(current_user["id"]), limit=limit))


@router.get("/analysis/tasks/{task_id}")
async def get_task(
    task_id: str,
    current_user: dict = Depends(get_current_user),
):
    store = get_store()
    task = store.get_task(int(current_user["id"]), task_id)
    if task is None:
        # 与越权同码：404，不泄露「该 task_id 是否存在」
        return err("NOT_FOUND", f"分析任务 {task_id} 不存在", 404)
    # P1-1b：附上引擎的实时节点进度。``None`` = 读不到（无绑定 / 未开始），
    # 客户端据此显示「排队中」而不是伪造进度。
    task["progress"] = _progress_payload(task)
    return ok(task)


@router.post("/analysis/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    current_user: dict = Depends(get_current_user),
):
    """取消一个进行中的分析任务（「退出分析」确认后调用）。

    语义：
    - 幂等：任务已是终态时直接返回当前快照，不报错；
    - 归属：与 ``GET /analysis/tasks/{id}`` 同码 404，不泄露存在性；
    - 中止：置任务为 ``cancelled`` + 通知引擎在**下一个节点边界**停下。
      正在执行的单个节点（如一次 LLM 调用）无法被线程池强行打断，
      但它完成后不会写项目、不会扣额度、不会再推进进度。
    """
    store = get_store()
    user_id = int(current_user["id"])
    task = store.get_task(user_id, task_id)
    if task is None:
        return err("NOT_FOUND", f"分析任务 {task_id} 不存在", 404)

    if task["status"] not in CareerlyStore.TERMINAL_TASK_STATUSES:
        # 先发取消信号，再落终态：后台任务若恰好在此刻结束，终态以
        # complete_task / fail_task 的写入为准（cancel_task 不会降级已终态）。
        event = _task_cancel_events.get(task_id)
        if event is not None:
            event.set()
        store.cancel_task(task_id)
        task = store.get_task(user_id, task_id) or task

    task["progress"] = _progress_payload(task)
    return ok(task)


# ── Resume baseline ────────────────────────────────────────────────────────
#
# ## 简历资产的两个入口（2026-10-02）
#
# | 端点 | 语义 | 解析？ | 写 profile？ |
# |---|---|---|---|
# | `POST /resume` | **提交一份简历资产**（文件 / 图片 / 文本） | ✅ 立即解析 | ✅ |
# | `PUT  /resume` | 仅更新基线文本（P3 preview，无解析） | ❌ | ❌ |
#
# ⚠️ **`POST /resume` 是唯一推荐的入口**。三种「输入方式」
# （粘贴文本 / 文件 / 截图 OCR）在这里汇合：它们只差**文本怎么读出来**，
# 之后的 `ResumeAgent` 解析与 `careerly_resumes` 落库是**同一段代码**。
# 不存在第二套 Profile 写入逻辑。
#
# ⚠️ **两者都不扣分析额度**（`CLAUDE.md` §5.2：上传简历不扣额度）。

#: 简历资产入口接受的文件类型。
#:
#: ⚠️ 与 Flutter `ResumeFileData.allowedExtensions` **必须一致**。
#: 历史包袱：`/resume/upload`（`app/api/router_app.py`）只收 pdf/docx/txt，
#: 而 Flutter 的选择器放行 `.doc` —— 两边对不上。这里统一以
#: **ResumeAgent 真能解析的格式**为准（`.doc` 是旧二进制格式，
#: python-docx 读不了，故不收）。
_ALLOWED_RESUME_SUFFIXES = frozenset(
    {".pdf", ".docx", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
)

#: 与 `CLAUDE.md` / Figma 一致的上限（10MB）。
_RESUME_MAX_BYTES = 10 * 1024 * 1024


@router.post("/resume")
async def create_resume_asset(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    current_user: dict = Depends(get_current_user),
):
    """提交一份简历资产 → 解析 → 建立 / 更新职业基线。

    ``multipart/form-data``，二选一：

    * ``file`` —— PDF / DOCX / TXT / 图片（截图）。图片走 OCR。
    * ``text`` —— 直接粘贴的简历文本。

    返回体与 ``GET /resume`` **同形**（``{text, profile, updatedAt}``），
    调用方拿到后无需再发一次 GET。

    ⚠️ **不扣分析额度**，**不建分析任务** —— 这不是分析，
    是 PRD §7.2 的「建立职业基线」。
    """
    store = get_store()
    user_id = int(current_user["id"])

    # ── 1. 取文本：文件与粘贴文本在这里合流 ────────────────────────────
    cleanup: tempfile.TemporaryDirectory | None = None
    agent = ResumeAgent()
    #: 原始文件名，记进版本卡（[ResumeVersion.fileName]）。粘贴文本时为 None。
    source_name: str | None = None
    original_pdf: bytes | None = None

    try:
        if file is not None and file.filename:
            source_name = file.filename
            suffix = Path(file.filename).suffix.lower()
            if suffix not in _ALLOWED_RESUME_SUFFIXES:
                return err(
                    "UNSUPPORTED_FORMAT",
                    f"不支持的文件类型 {suffix or '(无扩展名)'}："
                    f"支持 {' / '.join(sorted(_ALLOWED_RESUME_SUFFIXES))}",
                    422,
                )

            content = await file.read()
            if not content:
                return err("VALIDATION_ERROR", "文件内容为空", 422)
            if len(content) > _RESUME_MAX_BYTES:
                return err("FILE_TOO_LARGE", "文件超过 10MB 上限", 413)
            if suffix == ".pdf":
                original_pdf = content

            # ResumeAgent 按路径读文件（pdfplumber / python-docx / PIL 都要路径），
            # 故先落临时文件，解析完立刻整目录删除。
            cleanup = tempfile.TemporaryDirectory(prefix="careerly-resume-")
            tmp_path = Path(cleanup.name) / f"resume{suffix}"
            tmp_path.write_bytes(content)

            profile = await agent.run(file_path=str(tmp_path), use_llm=True)

        elif text is not None and text.strip():
            profile = await agent.run(text=text.strip(), use_llm=True)

        else:
            return err("VALIDATION_ERROR", "请提供 file 或 text 其中之一", 422)

    except ResumeExtractionError as exc:
        logger.warning("Careerly resume asset failed | user=%d | code=%s | %s",
                       user_id, exc.code, exc.message)
        return err(exc.code, exc.message, 422)
    except Exception as exc:  # pragma: no cover - unexpected extractor crash
        logger.exception("Careerly resume asset crashed | user=%d", user_id)
        return err("PARSE_ERROR", f"简历解析失败：{exc}", 500)
    finally:
        if cleanup is not None:
            cleanup.cleanup()

    resume_text = (agent.raw_text or "").strip()
    if not resume_text:
        # ResumeAgent 只在文本真的为空时才会走到这里，属防御性兜底。
        return err("EMPTY_EXTRACTION", "未能提取到简历文本", 422)

    profile_dump = (
        profile.model_dump() if hasattr(profile, "model_dump") else {}
    )

    # ── 2. 落库：**追加一个新版本**（PRD §14 / Figma `ResumeHistoryPage`）──
    #
    # ⚠️ 这里是 `add_resume_version` 而不是 `save_resume` —— 两者差别是
    # 整个多版本功能的支点：
    #   add_resume_version   用户又交了一份新简历 → 版本表**多一行**
    #   save_resume          当前这份简历被刷新 → 原地改，不动版本表
    # 用错就会退回「上传后覆盖旧的、历史列表永远只有一张卡」的老行为。
    #
    # ⚠️ **只在画像「有结构」时采用新 profile** —— 沿用 `POST /analysis`
    # 同一条纪律（见该处注释）：解析偶尔会降级成只有 raw_text 的空壳，
    # 用空壳当画像会让「我的 → 职业资产」突然清零。
    # 但**版本照样要记** —— 用户确实交了这份简历，历史里该有它。
    previous = store.get_resume(user_id) or {}
    structured = any(
        profile_dump.get(key)
        for key in ("skills", "roles", "projects", "education", "certifications")
    )
    if not structured:
        logger.warning(
            "Careerly resume asset produced no structured profile | user=%d "
            "| keeping previous profile", user_id,
        )

    version = store.add_resume_version(
        user_id,
        resume_text,
        profile_dump if structured else (previous.get("profile") or {}),
        file_name=source_name,
        original_pdf=original_pdf,
    )
    saved = store.get_resume(user_id) or {}
    logger.info(
        "Careerly resume asset saved | user=%d | chars=%d | structured=%s | version=%s",
        user_id, len(resume_text), structured, version.get("label"),
    )
    return ok({
        "text": saved.get("text", resume_text),
        "profile": saved.get("profile", {}),
        "updatedAt": saved.get("updatedAt"),
        "version": version,
        "versions": store.list_resume_versions(user_id),
    })


@router.get("/resume/versions/{version_id}/original")
async def get_resume_version_original_pdf(
    version_id: str,
    current_user: dict = Depends(get_current_user),
):
    """读取当前用户指定简历版本上传的原始 PDF。"""
    original = get_store().get_resume_version_pdf(
        int(current_user["id"]), version_id
    )
    if original is None:
        return err("NOT_FOUND", "该版本没有可用的原始 PDF", 404)

    file_name, content = original
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f"inline; filename*=UTF-8''{quote(file_name, safe='')}"
            ),
            "Cache-Control": "private, no-store",
        },
    )


@router.put("/resume")
async def save_resume(
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """Persist the resume baseline text used by every analysis (P3 preview).

    ⚠️ **Does not touch ``profile``**. Saving new text here must not wipe the
    parsed profile — otherwise the text would silently reset
    「我的 → 职业资产」to empty, and re-saving identical text would look like
    the user lost their assets.

    ⚠️ **这不是建立职业资产的路径**。``profile`` 有两个写入者，都在别处：

    | 写入者 | 时机 |
    |---|---|
    | ``POST /api/v1/resume`` | 用户提交简历资产（文件 / 图片 / 文本）时**立即**解析 |
    | ``POST /api/v1/analysis`` | 分析链路内顺带解析（P3-1） |

    （2026-10-02 修订：本条原写「``POST /analysis`` 是唯一解析方」。新增
    简历资产入口后不再成立 —— 只走完 Onboarding、没做过分析的新用户，
    其「职业资产」也需要有真实数据。）
    """
    text = (body.get("text") or "").strip() if isinstance(body, dict) else ""
    if not text:
        return err("VALIDATION_ERROR", "text 不能为空", 422)
    store = get_store()
    user_id = int(current_user["id"])
    existing = store.get_resume(user_id) or {}
    store.save_resume(user_id, text, existing.get("profile") or {})
    saved = store.get_resume(user_id)
    return ok({"text": text, "profile": saved.get("profile", {}),
               "updatedAt": saved.get("updatedAt")})


@router.get("/resume")
async def get_resume(current_user: dict = Depends(get_current_user)):
    """当前简历（文本 + 画像）+ **全部版本**。

    ``versions`` 是「我的简历」列表的数据来源：``isCurrent`` 那条进顶部
    横幅，其余进「历史版本 · 左滑操作」。此前这个端点只返回一份基线，
    Flutter 侧只能**合成**一个假版本（``id: 'current'``）——
    于是「上传新简历」在界面上永远长不出第二张卡。
    """
    store = get_store()
    user_id = int(current_user["id"])
    saved = store.get_resume(user_id)
    if saved is None:
        return ok(None)  # no baseline yet — Dart treats null as "hasBaseline=false"
    return ok({**saved, "versions": store.list_resume_versions(user_id)})


@router.put("/resume/versions/{version_id}/current")
async def set_current_resume_version(
    version_id: str,
    current_user: dict = Depends(get_current_user),
):
    """把某个历史版本设为当前简历（PRD §14）。

    ⚠️ 会**同时改写分析用的基线**（``careerly_resumes``）—— 这正是
    「换了当前简历，下次分析用的是新的那份」的落点。
    """
    store = get_store()
    user_id = int(current_user["id"])
    if not store.set_current_resume_version(user_id, version_id):
        return err("NOT_FOUND", "指定的简历版本不存在", 404)
    return ok({"versions": store.list_resume_versions(user_id)})


@router.delete("/resume/versions/{version_id}")
async def delete_resume_version(
    version_id: str,
    current_user: dict = Depends(get_current_user),
):
    """删除一个**历史**版本（PRD §14）。

    ``409`` 表示该版本是当前简历 —— **当前简历不可删**。
    Figma 的删除按钮只出现在带「归档」胶囊的历史卡上（`SwipeableResumeCard`），
    而且删掉当前简历会让分析链路失去基线来源。用户在界面上也走不到这条路径，
    这里拒绝是为了让「数据层自己站得住」，不是靠 UI 拦着。
    """
    store = get_store()
    user_id = int(current_user["id"])
    result = store.delete_resume_version(user_id, version_id)
    if result == "not_found":
        return err("NOT_FOUND", "指定的简历版本不存在", 404)
    if result == "current":
        return err("VALIDATION_ERROR", "当前简历不能删除，请先设为其他版本", 409)
    return ok({"versions": store.list_resume_versions(user_id)})


# ── Quota ──────────────────────────────────────────────────────────────────


@router.get("/quota")
async def get_quota(current_user: dict = Depends(get_current_user)):
    return ok(dump(get_store().get_quota(int(current_user["id"]))))


# ── Growth（P2-4：成长足迹只读面）──────────────────────────────────────────
#
# 事件由「相邻快照 diff」在分析成功时派生（growth.py），这里只做
# 按 user 作用域的查询。type 必须是 Dart enum 名 —— 非法值 422，
# 绝不静默忽略（静默会让前端以为「没有这类事件」）。


@router.get("/growth/events")
async def list_growth_events(
    type: str | None = None,
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    store = get_store()
    if type is not None and type not in growth.VALID_TYPES:
        return err(
            "INVALID_TYPE",
            f"未知成长事件类型：{type}（可选：{'/'.join(sorted(growth.VALID_TYPES))}）",
            422,
        )
    return ok(store.list_growth_events(int(current_user["id"]), type=type, limit=limit))


@router.get("/growth/timeline")
async def growth_timeline(
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    limit: int = 200,
    current_user: dict = Depends(get_current_user),
):
    store = get_store()
    from_ts = _parse_iso(from_)
    to_ts = _parse_iso(to)
    if from_ is not None and from_ts is None:
        return err("INVALID_RANGE", f"from 不是合法 ISO-8601 时间：{from_}", 422)
    if to is not None and to_ts is None:
        return err("INVALID_RANGE", f"to 不是合法 ISO-8601 时间：{to}", 422)
    if from_ts is not None and to_ts is not None and from_ts > to_ts:
        return err("INVALID_RANGE", "from 不能晚于 to", 422)
    return ok(store.list_growth_events_range(
        int(current_user["id"]), from_ts=from_ts, to_ts=to_ts, limit=limit,
    ))


def _parse_iso(value: str | None) -> float | None:
    """ISO-8601 string → unix seconds; None/unparseable → None."""
    if not value:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ── Career assets / home / profile（P3：聚合读面）────────────────────────────
#
# 三个端点都是**只读聚合**：从已有的事实（简历画像、快照、分析结果、认证行）
# 组装 Flutter 需要的形状，不引入新的写入面，也不调用 LLM。
# 每个字段的来源与缺失时的行为，见 app/careerly/{assets,home,identity}.py 的模块注释。


@router.get("/career/assets")
async def get_career_assets(current_user: dict = Depends(get_current_user)):
    """「我的 → 职业资产」（P3-1）。

    数据源：``POST /analysis`` 落库的简历画像 + 最新的能力快照。
    ⚠️ 从未分析过 → 返回 ``CareerAssets`` 空态（不是 404）：
    「还没有职业资产」是合法状态，前端据此渲染空态页。
    """
    return ok(dump(career_assets.build_career_assets(get_store(), int(current_user["id"]))))


@router.get("/home/summary")
async def get_home_summary(current_user: dict = Depends(get_current_user)):
    """首页摘要（P3-2）。

    建议来自**最新分析已算出的 Gap actions** —— 首页不再额外花一次 LLM 调用，
    也不产出没有数据支撑的文案。
    """
    return ok(dump(home.build_home_summary(get_store(), int(current_user["id"]))))


@router.get("/profile")
async def get_profile(current_user: dict = Depends(get_current_user)):
    """「我的」身份资料（P3-3）。

    ⚠️ 手机号**脱敏**下发（``138****0000``）：这个字段会渲染在屏幕上，
    原样回显等于把完整身份标识写进每一次截图与录屏。
    """
    from app.core.auth_store import AuthStore

    user_id = int(current_user["id"])
    auth_user = AuthStore().get_by_id(user_id)
    quota = get_store().get_quota(user_id)
    return ok(dump(identity.build_user_profile(auth_user, quota)))


# ── AI advisor（P-AI-1）────────────────────────────────────────────────────
#
# 裁决（2026-10-03）：Current Context / 客户端会话历史 / 独立额度 / 流式 SSE。
# Historical Context 本轮不做（`ai_context.py` 的模块注释有边界说明）。
#
# ⚠️ 这是**唯一**响应不是 envelope 的 Careerly 端点 —— 流一旦开始，HTTP
# 状态码就已经发出去了，没法再改成 403/404。因此所有**前置失败**
# （额度、项目不存在、LLM 未配置）都必须在构造 StreamingResponse **之前**
# 以普通 `err(...)` 返回；只有**生成中途**的失败才走 SSE error 事件。
# 这个顺序与 `/match/stream` 一致（见 router_v1.py 的说明）。
#
# SSE 帧格式（前后端同一份约定）：
#     {"type": "start"}                        流已开始
#     {"type": "delta", "text": "…"}           增量文本，可多次
#     {"type": "done"}                         正常结束
#     {"type": "error", "code": "…", "message": "…"}   中途失败


def _sse(payload: dict[str, Any]) -> str:
    """一条 SSE 帧。不转义非 ASCII —— 中文在抓包里要能直读。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.post("/ai/messages", response_model=None)
async def ai_messages(
    req: AiMessageRequest,
    current_user: dict = Depends(get_current_user),
):
    """AI 职业顾问：接收一个问题，流式返回回答。

    上下文由**服务端**按 ``projectId`` 组装（见 ``ai_context.py``），
    客户端**不**上传 Career Context —— 只带一个 id，避免拿到一份
    几个月前的旧快照（裁决 2026-10-03 ②）。
    """
    store = get_store()
    user_id = int(current_user["id"])

    # ── 前置检查：全部在开流之前完成 ────────────────────────────────────
    ai_quota = store.get_ai_quota(user_id)
    if not ai_quota.isMember and ai_quota.used >= ai_quota.total:
        return err(
            "AI_QUOTA_EXCEEDED",
            "AI 对话次数已用完，升级会员后可继续使用",
            403,
            details={"quota": dump(ai_quota)},
        )

    context = build_current_context(store, user_id, req.projectId, req.jobId)
    if context is None and req.projectId:
        # 显式带参却查不到 → 报错，**不**静默回退到最近一次分析：
        # 否则用户以为在问 A 岗位，回答用的却是另一个项目的数据。
        return err("PROJECT_NOT_FOUND", f"分析项目 {req.projectId} 不存在", 404)

    try:
        llm = LLMService()
    except Exception as exc:
        # 构造期就会因缺 API key 抛错（llm_service.py）。这是**部署配置**
        # 问题，不是请求问题 —— 503 比 500 更诚实。
        logger.error(
            "Careerly ai/messages: LLM unavailable | user=%d | %s", user_id, exc
        )
        return err("AI_UNAVAILABLE", f"AI 服务未就绪：{exc}", 503)

    user_prompt = build_user_prompt(context, req.message)
    history = [turn.model_dump() for turn in req.history]

    logger.info(
        "Careerly ai/messages | user=%d | project=%s | job=%s | history=%d",
        user_id,
        (context or {}).get("analysis", {}).get("projectId"),
        req.jobId,
        len(history),
    )

    return StreamingResponse(
        _ai_stream(llm, user_prompt, history, store, user_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _ai_stream(
    llm: LLMService,
    user_prompt: str,
    history: list[dict],
    store: CareerlyStore,
    user_id: int,
):
    """把**同步**的 LLM 流桥接成 async SSE 生成器。

    `LLMService` 用的是同步 OpenAI 客户端，直接在事件循环里
    `for … in chat_stream()` 会把整个进程卡住（期间无法处理任何其它请求）。
    因此放进线程执行，用 `asyncio.Queue` + `call_soon_threadsafe` 把增量
    搬回事件循环 —— 与 `_progress_generator` 桥接同步引擎进度同一手法。
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    def produce() -> None:
        try:
            for text in llm.chat_stream(SYSTEM_PROMPT, user_prompt, history=history):
                loop.call_soon_threadsafe(queue.put_nowait, ("delta", text))
            loop.call_soon_threadsafe(queue.put_nowait, ("end", None))
        except Exception as exc:  # 中途失败：只能以 SSE error 事件收尾
            loop.call_soon_threadsafe(queue.put_nowait, ("error", str(exc)))

    yield _sse({"type": "start"})
    loop.run_in_executor(None, produce)

    failed = False
    while True:
        kind, payload = await queue.get()
        if kind == "delta":
            yield _sse({"type": "delta", "text": payload})
        elif kind == "error":
            logger.error(
                "Careerly ai stream failed | user=%d | %s", user_id, payload
            )
            yield _sse(
                {
                    "type": "error",
                    "code": "AI_FAILED",
                    "message": f"AI 回复中断：{payload}",
                }
            )
            failed = True
            break
        else:
            break

    if not failed:
        # 成功才扣 —— 与 consume_quota 同一纪律（用户没拿到回答不该付次数）。
        store.consume_ai_quota(user_id)
        yield _sse({"type": "done"})
