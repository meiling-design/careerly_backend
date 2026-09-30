"""Careerly API endpoints — the contract-aligned surface for the Flutter app.

Wire contract: ``docs/careerly/BACKEND_API_CONTRACT.md`` (§1/§2).  Every JSON
shape is dictated by the Dart ``fromJson``/``toJson`` — do not rename fields.

Endpoints (all require a JWT via ``get_current_user``):

    POST /api/v1/analysis                    analyze({title, rawJd}) — synchronous
    GET  /api/v1/analysis/projects           list analysis projects
    GET  /api/v1/analysis/projects/{pid}     one project (jobs + versioned results)
    GET  /api/v1/analysis/tasks              list own analysis tasks (P1-1a 可观测)
    GET  /api/v1/analysis/tasks/{tid}        one task (ownership-scoped)
    PUT  /api/v1/resume                      save resume baseline text (P3 preview)
    GET  /api/v1/resume                      read resume baseline
    GET  /api/v1/quota                       QuotaState (Baseline §5: free 3)

Analysis is synchronous (decision C2): the engine call is offloaded to a
thread executor so the event loop stays responsive during the 10–20s run.
"""

from __future__ import annotations

import asyncio
from functools import partial

from fastapi import APIRouter, Depends

from app.api.envelope import err, ok
from app.core.security import get_current_user
from app.careerly.engine import AnalysisError, run_analysis
from app.careerly.schemas import AnalyzeRequest, dump
from app.careerly.store import CareerlyStore
from app.utils.logger import logger

router = APIRouter(prefix="/api/v1", tags=["careerly"])

_store: CareerlyStore | None = None


def get_store() -> CareerlyStore:
    """Lazy singleton; tests may replace ``router._store``."""
    global _store
    if _store is None:
        _store = CareerlyStore()
    return _store


# ── Analysis ────────────────────────────────────────────────────────────────


@router.post("/analysis")
async def analyze(
    req: AnalyzeRequest,
    current_user: dict = Depends(get_current_user),
):
    """Run one Gap analysis for a pasted JD and return the full project."""
    store = get_store()
    user_id = int(current_user["id"])

    # ── P1-1a：任务留痕。**在一切校验之前**建任务。
    #
    # 为什么必须在最前面：配额不足、缺少基线这类前置拒绝恰恰是最常见的失败，
    # 若等校验通过才建任务，它们就一个记录都不会留下 —— 「失败无痕」正是
    # P1-2 要消灭的东西。前置拒绝同样记 failed（started_at 保持 NULL，
    # 表示引擎从未真正启动）。
    #
    # 对外语义不变：仍等待完成后返回结果，Flutter 零改动（D-A ②第一阶段）。
    task_id = store.create_task(user_id, req.title, source="paste")

    # Quota gate (Baseline §5 / PRD §16): free users get 3 analyses total.
    quota = store.get_quota(user_id)
    if not quota.isMember and quota.used >= quota.total:
        store.fail_task(task_id, "QUOTA_EXCEEDED", "免费分析次数已用完")
        return err(
            "QUOTA_EXCEEDED",
            "免费分析次数已用完，升级会员后可继续分析",
            403,
            details={"quota": dump(quota)},
        )

    # Resume source: inline text wins, else the persisted baseline (P3).
    resume_text = (req.resumeText or "").strip()
    if not resume_text:
        baseline = store.get_resume(user_id)
        resume_text = (baseline or {}).get("text", "").strip()
    if not resume_text:
        # P1-2：与 engine.run_analysis 的 NO_BASELINE 保持同一错误码，
        # 否则「内联简历缺失」和「基线缺失」会给出两个不同的 code，前端无法统一处理。
        message = "尚未建立职业基线：请先上传简历或传入 resumeText"
        store.fail_task(task_id, "NO_BASELINE", message)
        return err("NO_BASELINE", message, 422)

    store.start_task(task_id)

    loop = asyncio.get_running_loop()
    try:
        project = await loop.run_in_executor(
            None,
            partial(run_analysis, title=req.title, raw_jd=req.rawJd, resume_text=resume_text),
        )
    except AnalysisError as exc:
        # P1-2：失败必须落库，消灭「失败无痕」。
        store.fail_task(task_id, exc.code, exc.message)
        logger.warning("Careerly analyze failed | user=%d | task=%s | code=%s",
                       user_id, task_id, exc.code)
        return err(exc.code, exc.message, exc.status_code)
    except Exception as exc:  # pragma: no cover - unexpected engine crash
        store.fail_task(task_id, "ANALYSIS_FAILED", str(exc))
        logger.exception("Careerly analyze crashed | user=%d | task=%s", user_id, task_id)
        return err("ANALYSIS_FAILED", f"分析失败：{exc}", 502)

    store.save_project(user_id, project)
    store.consume_quota(user_id)
    store.complete_task(task_id, project.id)
    logger.info("Careerly analyze | user=%d | project=%s | task=%s | quota=%d/%d",
                user_id, project.id, task_id, quota.used + 1, quota.total)

    return ok(dump(project), status_code=201)


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


# ── Analysis tasks（P1-1a 可观测面）────────────────────────────────────────
#
# 只读、按 user 作用域。这里**不**提供 202 + 轮询 —— 那属于 P1-1b，
# 等状态机稳定后再引入，避免一次性搅动 Flutter / API / Agent 三层。


@router.get("/analysis/tasks")
async def list_tasks(
    limit: int = 50,
    current_user: dict = Depends(get_current_user),
):
    """本人最近的分析任务（含失败原因与耗时）。"""
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
    return ok(task)


# ── Resume baseline ────────────────────────────────────────────────────────


@router.put("/resume")
async def save_resume(
    body: dict,
    current_user: dict = Depends(get_current_user),
):
    """Persist the resume baseline text used by every analysis (P3 preview)."""
    text = (body.get("text") or "").strip() if isinstance(body, dict) else ""
    if not text:
        return err("VALIDATION_ERROR", "text 不能为空", 422)
    store = get_store()
    user_id = int(current_user["id"])
    store.save_resume(user_id, text)
    saved = store.get_resume(user_id)
    return ok({"text": text, "profile": saved.get("profile", {}),
               "updatedAt": saved.get("updatedAt")})


@router.get("/resume")
async def get_resume(current_user: dict = Depends(get_current_user)):
    saved = get_store().get_resume(int(current_user["id"]))
    if saved is None:
        return ok(None)  # no baseline yet — Dart treats null as "hasBaseline=false"
    return ok(saved)


# ── Quota ──────────────────────────────────────────────────────────────────


@router.get("/quota")
async def get_quota(current_user: dict = Depends(get_current_user)):
    return ok(dump(get_store().get_quota(int(current_user["id"]))))
