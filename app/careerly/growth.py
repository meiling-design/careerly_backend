"""Growth derivation: turn capability snapshots into growth events (P2-3).

The snapshot table records *what I look like now*; this module derives
*what changed* by diffing the newest snapshot against the previous one.

Boundary decisions (confirmed 2026-10-01):
* First analysis → one ``experience`` event「建立职业基线」(there is no
  "previous" to diff against, but the user should not see an empty footprint).
* ``rank`` events are **not** generated — one analysis = one job, there is no
  "same-job rank change" semantics; fabricating it would be fake data.
* ``experience`` / ``skill`` types stay in the enum but are not generated here
  (their data source is career assets, still Mock — see contract P2-6 note).
* Dimensions present in the previous snapshot but missing now stay silent:
  capability sets vary with JD wording, and removal is noise, not regression.
"""

from __future__ import annotations

import time
from typing import Any, Optional

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


def build_scores_payload(project: AnalysisProject) -> dict[str, Any]:
    """Snapshot payload from a finished analysis.

    Shape: ``{"capabilities": {name: score}, "matchScore": int | None}``.
    Capabilities may be empty when C4 scoring degraded — the snapshot is
    still written so every successful analysis leaves an anchor.
    """
    capabilities: dict[str, int] = {}
    match_score: Optional[int] = None
    if project.results:
        result = project.results[-1]
        capabilities = dict(result.capabilitySnapshot or {})
        if result.matches:
            top = min(result.matches, key=lambda m: m.rank or 1)
            match_score = top.score
    return {"capabilities": capabilities, "matchScore": match_score}


def diff_snapshots(prev: Optional[dict[str, Any]], new_scores: dict[str, Any],
                   project_name: str, now: float) -> list[dict[str, Any]]:
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
    prev_caps: dict[str, int] = (prev.get("scores") or {}).get("capabilities") or {}
    new_caps: dict[str, int] = new_scores.get("capabilities") or {}

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

    prev_ms = (prev.get("scores") or {}).get("matchScore")
    new_ms = new_scores.get("matchScore")
    if prev_ms is not None and new_ms is not None and new_ms != prev_ms:
        events.append({
            "type": TYPE_MATCH_SCORE,
            "title": "岗位匹配度变化",
            "description": f"{prev_ms} → {new_ms}",
            "delta": new_ms - prev_ms,
            "occurred_at": now,
        })

    return events


def record_analysis_growth(store: Any, user_id: int, project: AnalysisProject) -> None:
    """Write the snapshot for a finished analysis and persist derived events.

    Defensive on purpose: growth recording must never turn a successful
    analysis into a 502 (same lesson as the C4 ``state.resume`` bug).
    Failures are logged and swallowed — the analysis itself already landed.
    """
    try:
        prev = store.get_latest_snapshot(user_id)
        scores = build_scores_payload(project)
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
