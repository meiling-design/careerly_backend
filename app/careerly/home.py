"""Home-summary assembly: 「看见现在」 in one payload (P3-2).

``GET /api/v1/home/summary`` is the first screen the user sees after login, so
every field must trace to something real. This module owns that traceability:

| HomeSummary field | Source | When absent |
|---|---|---|
| ``headline`` | latest analysis capability set | neutral fallback |
| ``subheadline`` | latest match score + capability count | neutral fallback |
| ``suggestions`` | latest ``AnalysisResult`` gaps' actions | ``[]`` |
| ``matchChanges`` | adjacent snapshots' ``matchScore`` diff | ``[]`` |
| ``recentGrowth`` | newest ``growth_events`` titles | ``[]`` |
| ``dailyContent`` | **no source** | ``null`` |
| ``hasBaseline`` | whether a snapshot exists | ``false`` |

⚠️ Two fields are deliberately restrained
------------------------------------------
* ``dailyContent`` is ``None``. The Figma prototype shows a daily card, but
  nothing in the backend produces one. A rotating placeholder would look
  "alive" while being pure decoration — the opposite of what this product is
  for. Left ``null``; Flutter hides the block.
* ``headline`` / ``subheadline`` fall back to *neutral* text (not advice) when
  there is no analysis yet. Inventing encouragement for a brand-new user would
  make the empty state indistinguishable from a real one.

⚠️ Where ``suggestions`` comes from, and why not from the LLM
-------------------------------------------------------------
The analysis already produced ``gapDetails[].actions`` — concrete, grounded in
the actual resume↔JD gap. Calling the LLM again on the home page would cost
money and latency per view for text that is already computed and more reliable.
"""

from __future__ import annotations

from typing import Any, Optional

from app.careerly.schemas import HomeSummary, MatchChange

# Neutral, non-advisory copy for users with no analysis yet.
_NO_BASELINE_HEADLINE = "先建立你的职业基线"
_NO_BASELINE_SUBHEADLINE = "完成一次分析，就能看到能力画像与成长变化"

_MAX_SUGGESTIONS = 3
_MAX_MATCH_CHANGES = 3
_MAX_RECENT_GROWTH = 3


def _latest_project(store: Any, user_id: int) -> Optional[Any]:
    projects = store.list_projects(user_id, limit=1)
    return projects[0] if projects else None


def _latest_result(project: Any) -> Optional[Any]:
    if project is None or not getattr(project, "results", None):
        return None
    return project.results[-1]


def _baseline_text(store: Any, user_id: int) -> str:
    saved = store.get_resume(user_id) or {}
    return (saved.get("text") or "").strip()


def _suggestions_from(project: Any) -> list[str]:
    """Gap actions from the newest analysis, deduped and capped.

    Ordering follows the engine's gap ordering (most severe first), so the
    home page shows the most pressing action — not an arbitrary one.
    """
    result = _latest_result(project)
    if result is None:
        return []
    out: list[str] = []
    for match in result.matches:
        for gap in match.gapDetails:
            for action in gap.actions:
                text = (action or "").strip()
                if text and text not in out:
                    out.append(text)
                    if len(out) >= _MAX_SUGGESTIONS:
                        return out
    # No numeric gap details (C4 degraded) — fall back to the match's own
    # suggestion, which the engine derives from the same gap data.
    if not out and result.matches:
        text = (result.matches[0].suggestion or "").strip()
        if text:
            out.append(text)
    return out


def _headline(project: Any, capabilities: list[dict]) -> tuple[str, str]:
    """``(headline, subheadline)`` — neutral when there is nothing to report."""
    if project is None:
        return _NO_BASELINE_HEADLINE, _NO_BASELINE_SUBHEADLINE

    result = _latest_result(project)
    score: Optional[int] = None
    if result is not None and result.matches:
        score = result.matches[0].score

    if score is None:
        return "已完成职业基线", "继续分析目标岗位，看能力与目标的差距"

    top_gap = ""
    if result is not None and result.matches:
        for gap in result.matches[0].gapDetails:
            name = (gap.name or "").strip()
            if name:
                top_gap = name
                break

    headline = f"当前与目标岗位的匹配度 {score} 分"
    if top_gap:
        subheadline = f"最需要补上的是「{top_gap}」"
    elif capabilities:
        subheadline = f"已识别 {len(capabilities)} 项能力维度"
    else:
        subheadline = "继续完善简历，让画像更准确"
    return headline, subheadline


def _match_changes(store: Any, user_id: int) -> list[MatchChange]:
    """Score movement between the two newest snapshots, joined to job titles.

    Only emitted when ``matchScore`` actually changed — a delta of 0 is not a
    "change" and showing it would pad the card with noise.
    """
    latest = store.get_latest_snapshot(user_id)
    if not latest:
        return []
    previous = store.get_previous_snapshot(user_id, latest["id"])
    if not previous:
        return []

    new_score = (latest.get("scores") or {}).get("matchScore")
    old_score = (previous.get("scores") or {}).get("matchScore")
    if not isinstance(new_score, int) or not isinstance(old_score, int):
        return []
    delta = new_score - old_score
    if delta == 0:
        return []

    # The snapshot's ``sourceId`` is the project id, which resolves to the job
    # the score belongs to. Without it we cannot honestly label the row.
    project_id = latest.get("sourceId")
    if not project_id:
        return []
    project = store.get_project(user_id, project_id)
    if project is None or not project.jobs:
        return []
    job = project.jobs[0]

    return [
        MatchChange(
            jobId=job.id,
            jobTitle=job.title,
            company=job.company or None,
            score=new_score,
            delta=delta,
        )
    ]


def _recent_growth(store: Any, user_id: int) -> list[str]:
    events = store.list_growth_events(user_id, limit=_MAX_RECENT_GROWTH)
    out: list[str] = []
    for ev in events:
        title = (ev.get("title") or "").strip()
        if title and title not in out:
            out.append(title)
    return out


def build_home_summary(store: Any, user_id: int) -> HomeSummary:
    """Assemble ``HomeSummary`` for one user."""
    project = _latest_project(store, user_id)
    capabilities = store.list_capabilities(user_id) if project is not None else []
    headline, subheadline = _headline(project, capabilities)

    return HomeSummary(
        headline=headline,
        subheadline=subheadline,
        suggestions=_suggestions_from(project),
        matchChanges=_match_changes(store, user_id),
        recentGrowth=_recent_growth(store, user_id),
        dailyContent=None,  # no source — see module docstring
        hasBaseline=bool(_baseline_text(store, user_id)),
    )
