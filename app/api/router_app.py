"""App 专用 API — resume upload / simplified result.

专为 Android / Web App 设计：

1. ``POST /api/v1/resume/upload`` — 上传 PDF 简历并解析，返回 resume_id
2. ``POST /api/v1/match``         — 复用已有接口（见 router_v1），App 将 resume_id
                                    解析为 resume_text 后调用即可
3. ``GET  /api/v1/tasks/{id}/result`` — 获取简化后的匹配结果（0-100 分、优势、差距、建议）

**推荐 App 调用流程**::

    # 1. 上传简历
    POST /api/v1/resume/upload  (multipart file)
    -> {"resume_id": "resume_xxx", "skills": [...]}

    # 2. 发起匹配（调用已有接口）
    POST /api/v1/match  (JSON body with resume_text + job_urls)
    -> {"task_id": "session-xxx", "status": "pending", ...}

    # 3. 轮询结果
    GET /api/v1/tasks/{session_id}/result
    -> {"score": 87, "advantages": [...], "gaps": [...], "suggestions": [...]}

响应遵循与 router_v1 一致的统一信封：

    .. code-block:: json

        {
            "success": true,
            "data": { ... },
            "error": null,
            "meta": { "timestamp": ..., "version": "v1" }
        }
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse

from app.agents.resume_agent import ResumeAgent
from app.core.security import get_current_user
from app.core.state_store import StateStore
from app.utils.logger import logger

# ── Router ──────────────────────────────────────────────────────────────────

router = APIRouter(prefix="/api/v1")

# ── File storage paths ──────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
_UPLOAD_DIR = _DATA_DIR / "uploads"
_RESUME_DIR = _DATA_DIR / "resumes"
_MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
_ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}

_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
_RESUME_DIR.mkdir(parents=True, exist_ok=True)

# ── Envelope helpers (mirror router_v1 conventions) ────────────────────────

_API_VERSION = "v1"


def _ok(data: Any = None, status_code: int = 200, meta_extra: dict | None = None) -> JSONResponse:
    meta: dict[str, Any] = {"timestamp": time.time(), "version": _API_VERSION}
    if meta_extra:
        meta.update(meta_extra)
    return JSONResponse(
        status_code=status_code,
        content={"success": True, "data": data, "error": None, "meta": meta},
    )


def _err(code: str, message: str, status_code: int = 400) -> JSONResponse:
    logger.warning("App API error | code=%s | status=%d | msg=%s", code, status_code, message)
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message},
            "meta": {"timestamp": time.time(), "version": _API_VERSION},
        },
    )


# ── Singleton helper ───────────────────────────────────────────────────────

_store: StateStore | None = None


def get_store() -> StateStore:
    global _store
    if _store is None:
        _store = StateStore()
    return _store


# ── Resume metadata persistence (file‑based) ───────────────────────────────

_RESUMES_CACHE: dict[str, dict[str, Any]] = {}
"""In-memory cache of resume metadata, backed by JSON files in ``_RESUME_DIR``."""


def _save_resume_meta(
    resume_id: str,
    filename: str,
    profile: Any,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Persist parsed resume metadata and return a lightweight summary.

    ``user_id`` 是归属字段，只由 API 层从 JWT 注入（决策 D-F），
    **绝不**取自请求体 —— 客户端无权指定自己属于谁。
    """
    profile_dict = profile.model_dump() if hasattr(profile, "model_dump") else profile
    data = {
        "resume_id": resume_id,
        "name": filename,
        "user_id": user_id,
        "profile": profile_dict,
        "created_at": time.time(),
    }
    path = _RESUME_DIR / f"{resume_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _RESUMES_CACHE[resume_id] = data

    return {
        "resume_id": resume_id,
        "name": filename,
        "skills": profile_dict.get("skills", []) if isinstance(profile_dict, dict)
                 else profile.skills if hasattr(profile, "skills")
                 else [],
    }


# ═══════════════════════════════════════════════════════════════════════════
#  Endpoints
# ═══════════════════════════════════════════════════════════════════════════


# ── 1. Resume Upload ──────────────────────────────────────────────────────


@router.post("/resume/upload")
async def upload_resume(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> JSONResponse:
    """上传并解析简历（PDF / DOCX / TXT）。

    流程：
    1. 接收 PDF 文件
    2. 使用 pdfplumber 提取文本
    3. 通过 ResumeAgent 进行结构化解析（含 LLM 增强）
    4. 保存解析结果
    5. 返回 resume_id 和技能列表

    **返回示例**:

    .. code-block:: json

        {
            "success": true,
            "data": {
                "resume_id": "resume_xxx",
                "name": "my_resume.pdf",
                "skills": ["Python", "AI产品"]
            }
        }
    """
    # 归属（P0-4）：一律取自 JWT，绝不信任请求体。
    user_id = str(current_user["id"])

    # ── Validate file ──────────────────────────────────────────────────
    if not file.filename:
        return _err("INVALID_FILE", "No file provided", 400)

    suffix = Path(file.filename).suffix.lower()
    if suffix not in _ALLOWED_EXTENSIONS:
        return _err(
            "UNSUPPORTED_FORMAT",
            f"Unsupported format '{suffix}'. Supported: PDF, DOCX, TXT",
            400,
        )

    # ── Save to temp location ──────────────────────────────────────────
    file_id = f"upload_{uuid.uuid4().hex[:12]}"
    temp_path = _UPLOAD_DIR / f"{file_id}{suffix}"

    try:
        content = await file.read()
        if len(content) > _MAX_FILE_SIZE:
            return _err("FILE_TOO_LARGE", "File size exceeds 10 MB limit", 413)

        temp_path.write_bytes(content)

        # ── Parse with ResumeAgent ─────────────────────────────────────
        agent = ResumeAgent()
        profile = await agent.run(file_path=str(temp_path), use_llm=True)

        # ── Persist metadata ───────────────────────────────────────────
        resume_id = f"resume_{uuid.uuid4().hex[:8]}"
        summary = _save_resume_meta(resume_id, file.filename, profile, user_id=user_id)

        logger.info("Resume uploaded & parsed | id=%s | name=%s | skills=%d",
                    resume_id, file.filename, len(profile.skills))
        return _ok(summary, status_code=200)

    except Exception as exc:
        logger.error("Resume upload failed | file=%s | %s", file.filename, exc)
        return _err("PARSE_ERROR", f"Failed to parse resume: {exc}", 500)

    finally:
        temp_path.unlink(missing_ok=True)


# ── 2. Task Result ────────────────────────────────────────────────────────


@router.get("/tasks/{task_id}/result")
async def get_task_result(
    task_id: str,
    current_user: dict = Depends(get_current_user),
) -> JSONResponse:
    """获取简化后的匹配结果（App 结果页展示用）。

    从已有的 v1 匹配 session 中读取结果，提取 top-1 职位的
    分数、优势、差距和改进建议，以 App 友好的格式返回。

    **返回字段**:

    * ``score`` — 匹配分数（0–100 整数）
    * ``advantages`` — 候选人的优势匹配项
    * ``gaps`` — 候选人的缺失技能 / 不足
    * ``suggestions`` — 改进建议

    **返回示例**:

    .. code-block:: json

        {
            "success": true,
            "data": {
                "score": 87,
                "advantages": ["AI产品经验", "用户体验设计"],
                "gaps": ["招聘行业经验不足"],
                "suggestions": ["强化招聘平台项目描述"]
            }
        }
    """
    store = get_store()
    # 归属校验（P0-4）：user_id 取自 JWT；归属不符时 get_session_info 返回 None，
    # 于是与「任务不存在」返回**同一个** 404 —— 不泄露资源存在性（决策 D-G）。
    info = store.get_session_info(task_id, user_id=str(current_user["id"]))

    if not info:
        return _err("NOT_FOUND", f"Task {task_id} not found", 404)

    # ── Not yet completed → return current status only ────────────────
    if info["status"] != "completed":
        return _ok({
            "task_id": task_id,
            "status": info["status"],
        })

    # ── Load results ──────────────────────────────────────────────────
    match_results = store.load_match_results(task_id)
    if not match_results:
        return _err("NO_RESULTS", "No match results available for this task", 404)

    # Take the top result by score
    top = sorted(match_results, key=lambda r: r.score, reverse=True)[0]

    return _ok({
        "score": round(top.score * 100),          # 0-1 → 0-100
        "advantages": top.skill_overlap[:10],
        "gaps": top.missing_skills[:10],
        "suggestions": top.recommendations[:5],
    })
