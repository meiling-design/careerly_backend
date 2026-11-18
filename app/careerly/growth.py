"""Growth derivation: turn capability snapshots into growth events (P2-3).

The snapshot table records *what I look like now*; this module derives
*what changed* by diffing the newest snapshot against a **comparable**
previous one.

Boundary decisions (confirmed 2026-10-01, revised 2026-11-18 for P1-03)
----------------------------------------------------------------------
* First analysis → one ``experience`` event「建立职业基线」(there is no
  "previous" to diff against, but the user should not see an empty footprint).
* ``rank`` events are **not** generated — one analysis = one job, there is no
  "same-job rank change" semantics; fabricating it would be fake data.
* ``experience`` / ``skill`` types stay in the enum but are not generated here
  (their data source is career assets, still Mock — see contract P2-6 note).
* Dimensions present in the previous snapshot but missing now stay silent:
  capability sets vary with JD wording, and removal is noise, not regression.

## P1-03：两类变化必须分开比较

```text
A. 职业能力变化    同一能力维度 + 同一评分口径 → capability 事件
B. 目标岗位匹配度  同一岗位 + 同一 JD + 不同简历 + 同一评分口径 → matchScore 事件
```

**不能**把「AI 产品经理 78 分」和「AI 产品运营 82 分」直接相减说成提升 4 分。
因此快照里额外记录：

* ``scoringVersion`` —— 能力评分口径版本（``capability.SCORING_VERSION``）。
  不一致 → 不比较。
* ``capabilitySignature`` —— 本次的能力维度集合（诊断用，不参与硬判定）。
* ``match`` —— ``{jobId, jobKey, jdHash, score}``。``jobKey`` 由岗位名 +
  公司名派生，``jdHash`` 是 JD 原文哈希；两者都相同才算「同一岗位、
  同一 JD」。
* ``resumeVersionId`` / ``resumeFingerprint`` —— 匹配度比较要求简历确实
  换过版本（没有换简历却分数变化，多半是评分波动，不是用户成长）。

历史快照没有这些字段 → 视为不可比较，**不生成**差值事件。
不伪造旧数据中不存在的成长记录。
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Optional

from app.careerly.capability import SCORING_VERSION
from app.careerly.schemas import AnalysisProject
from app.utils.logger import logger

# Dart enum names (growth_event.dart) — never emit Chinese labels here.
TYPE_EXPERIENCE = "experience"
TYPE_SKILL = "skill"
TYPE_CAPABILITY = "capability"
TYPE_MATCH_SCORE = "matchScore"
TYPE_RANK = "rank"

VALID_TYPES = {TYPE_EXPERIENCE, TYPE_SKILL, TYPE_CAPABILITY, TYPE_MATCH_SCORE, TYPE_RANK}

BASELINE_TITLE = "建立职业基线"


def _fingerprint(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:32]


def job_key(title: str, company: str) -> str:
    """稳定的「目标岗位」标识（P1-03 B）。

    当前每次分析都会新建一个 project / job 行，数据库 id 每次都不同，
    不能拿它判断「是不是同一个目标岗位」。这里用**归一化后的岗位名 +
    公司名**派生 key：同名同公司 = 同一目标岗位；改名 = 另一个目标岗位。
    """
    normalized = f"{(title or '').strip().lower()}\n{(company or '').strip().lower()}"
    return _fingerprint(normalized)


def _snapshot_meta(project: AnalysisProject) -> dict[str, Any]:
    """从分析结果里取「本次分析实际用了哪份 JD」。"""
    if not project.results:
        return {}
    result = project.results[-1]
    snap = next(
        (s for s in result.jobSnapshots if getattr(s, "available", False)), None
    )
    job = None
    if snap is not None:
        job = next((j for j in project.jobs if j.id == snap.jobId), None)
    if job is None and project.jobs:
        job = project.jobs[0]
    return {
        "job": job,
        "snapshot": snap,
        "title": (snap.title if snap is not None else (job.title if job else "")),
        "company": (snap.company if snap is not None else (job.company if job else "")),
        "rawJd": (snap.rawJd if snap is not None else (job.rawJd if job else "")),
        "jobId": (snap.jobId if snap is not None else (job.id if job else "")),
    }


def build_scores_payload(
    project: AnalysisProject,
    *,
    resume_version_id: str | None = None,
    resume_version_label: str | None = None,
    resume_text: str = "",
) -> dict[str, Any]:
    """Snapshot payload from a finished analysis.

    Shape::

        {
          "capabilities": {name: score},
          "matchScore": int | None,
          "scoringVersion": "c4-cap-v1",
          "capabilitySignature": ["..."],
          "match": {"jobId", "jobKey", "jdHash", "score"} | None,
          "resumeVersionId": str | None,
          "resumeVersionLabel": str | None,
          "resumeFingerprint": str,
        }

    Capabilities may be empty when C4 scoring degraded — the snapshot is
    still written so every successful analysis leaves an anchor.
    """
    capabilities: dict[str, int] = {}
    match_score: Optional[int] = None
    match_meta: Optional[dict[str, Any]] = None
    result = project.results[-1] if project.results else None
    if result is not None:
        capabilities = dict(result.capabilitySnapshot or {})
        if result.matches:
            top = min(result.matches, key=lambda m: m.rank or 1)
            match_score = top.score
            meta = _snapshot_meta(project)
            match_meta = {
                "jobId": meta.get("jobId") or top.jobId,
                "jobKey": job_key(meta.get("title", ""), meta.get("company", "")),
                "jdHash": _fingerprint(meta.get("rawJd", "")),
                "jobTitle": meta.get("title", ""),
                "company": meta.get("company", ""),
                "score": top.score,
            }

    return {
        "capabilities": capabilities,
        "matchScore": match_score,
        "scoringVersion": SCORING_VERSION,
        "capabilitySignature": sorted(capabilities.keys()),
        "match": match_meta,
        "resumeVersionId": resume_version_id
        or (result.resumeVersionId if result is not None else None),
        "resumeVersionLabel": resume_version_label
        or (result.resumeVersionLabel if result is not None else None),
        "resumeFingerprint": _fingerprint(resume_text),
    }


def capability_scores_comparable(
    prev_scores: dict[str, Any], new_scores: dict[str, Any]
) -> bool:
    """A 类：能力分是否来自同一评分口径（P1-03）。"""
    prev_version = prev_scores.get("scoringVersion")
    return bool(prev_version) and prev_version == new_scores.get("scoringVersion")


def match_score_comparable(
    prev_scores: dict[str, Any], new_scores: dict[str, Any]
) -> bool:
    """B 类：岗位匹配度是否可比较（P1-03）。

    必须同时满足：

    1. 同一目标岗位（``jobKey`` 相同）；
    2. 同一 JD 内容（``jdHash`` 相同）；
    3. 不同简历（``resumeFingerprint`` 有值且不同）；
    4. 同一评分口径（``scoringVersion`` 相同）。
    """
    if not capability_scores_comparable(prev_scores, new_scores):
        return False
    prev_match = prev_scores.get("match")
    new_match = new_scores.get("match")
    if not isinstance(prev_match, dict) or not isinstance(new_match, dict):
        return False
    if not prev_match.get("jobKey") or prev_match.get("jobKey") != new_match.get("jobKey"):
        return False
    if not prev_match.get("jdHash") or prev_match.get("jdHash") != new_match.get("jdHash"):
        return False
    prev_resume = prev_scores.get("resumeFingerprint")
    new_resume = new_scores.get("resumeFingerprint")
    return bool(prev_resume) and bool(new_resume) and prev_resume != new_resume


def diff_snapshots(
    prev: Optional[dict[str, Any]],
    new_scores: dict[str, Any],
    project_name: str,
    now: float,
) -> list[dict[str, Any]]:
    """Derive growth events from two adjacent snapshots.

    ``prev`` is the store row of the previous snapshot (or None for the
    user's very first analysis); ``new_scores`` is the payload about to be
    persisted. Returns event dicts ready for ``store.save_growth_events``.
    """
    if prev is None:
        return [{
            "type": TYPE_EXPERIENCE,
            "title": BASELINE_TITLE,
            "description": f"完成首次分析「{project_name}」，成长从这里开始记录",
            "delta": None,
            "occurred_at": now,
        }]

    events: list[dict[str, Any]] = []
    prev_scores: dict[str, Any] = prev.get("scores") or {}
    prev_caps: dict[str, int] = prev_scores.get("capabilities") or {}
    new_caps: dict[str, int] = new_scores.get("capabilities") or {}

    # A 类：职业能力变化。口径不一致 → 完全是两套数字，不生成任何
    # 提升 / 下降事件（新维度也不再宣布「纳入画像」，避免虚增）。
    if capability_scores_comparable(prev_scores, new_scores):
        for name, score in new_caps.items():
            if name not in prev_caps:
                events.append({
                    "type": TYPE_CAPABILITY,
                    "title": f"{name}能力纳入画像",
                    "description": f"首次评估 {score} 分",
                    "delta": None,
                    "occurred_at": now,
                })
            elif score != prev_caps[name]:
                delta = score - prev_caps[name]
                events.append({
                    "type": TYPE_CAPABILITY,
                    "title": f"{name}能力{'提升' if delta > 0 else '下降'}",
                    "description": f"{prev_caps[name]} → {score}",
                    "delta": delta,
                    "occurred_at": now,
                })

    # B 类：目标岗位匹配度变化。只有同一岗位 + 同一 JD + 不同简历才允许。
    prev_ms = prev_scores.get("matchScore")
    new_ms = new_scores.get("matchScore")
    if (
        isinstance(prev_ms, int)
        and isinstance(new_ms, int)
        and prev_ms != new_ms
        and match_score_comparable(prev_scores, new_scores)
    ):
        events.append({
            "type": TYPE_MATCH_SCORE,
            "title": "岗位匹配度变化",
            "description": f"{prev_ms} → {new_ms}",
            "delta": new_ms - prev_ms,
            "occurred_at": now,
        })

    return events


def record_analysis_growth(
    store: Any,
    user_id: int,
    project: AnalysisProject,
    *,
    resume_text: str = "",
    resume_version_id: str | None = None,
    resume_version_label: str | None = None,
) -> None:
    """Write the snapshot for a finished analysis and persist derived events.

    Defensive on purpose: growth recording must never turn a successful
    analysis into a 502 (same lesson as the C4 ``state.resume`` bug).
    Failures are logged and swallowed — the analysis itself already landed.
    """
    try:
        prev = store.get_latest_snapshot(user_id)
        scores = build_scores_payload(
            project,
            resume_version_id=resume_version_id,
            resume_version_label=resume_version_label,
            resume_text=resume_text,
        )
        events = diff_snapshots(prev, scores, project.name, time.time())
        snapshot_id = store.save_snapshot(
            user_id, source="analysis", source_id=project.id, scores=scores,
        )
        store.save_growth_events(user_id, events)
        logger.info(
            "Careerly growth | user=%d | snapshot=%s | events=%d",
            user_id, snapshot_id, len(events),
        )
    except Exception:  # pragma: no cover - must not break analyze()
        logger.exception("Careerly growth recording failed | user=%d", user_id)
