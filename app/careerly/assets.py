"""Career-asset assembly: turn the parsed resume profile into ``CareerAssets`` (P3-1).

``GET /api/v1/career/assets`` serves 「我的 → 职业资产」. Its data source is the
**parsed resume profile** persisted by ``POST /analysis`` (see
``app/careerly/router.py``) — the engine already parsed the resume for free
inside the analysis the user paid for, so re-parsing here would mean an LLM
call per page view.

⚠️ The honest-mapping rule
--------------------------
The engine's ``ResumeProfile`` is much thinner than Flutter's ``CareerAssets``.
Rather than invent plausible-looking values, every field the parser does not
produce is left at its empty default, and the mapping is documented below:

| CareerAssets field | Source | When absent |
|---|---|---|
| ``workExperiences[].title`` | ``roles[]`` | row not emitted |
| ``workExperiences[].company`` | **not parsed** | ``""`` |
| ``workExperiences[].period`` | **not parsed** | ``""`` |
| ``workExperiences[].description`` | **not parsed** | ``""`` |
| ``projects[].name`` | ``projects[].name`` | — |
| ``projects[].description`` | ``projects[].description`` | ``""`` |
| ``projects[].outcome`` | ``projects[].highlights[0]`` | ``""`` |
| ``projects[].skills`` | ``projects[].tech_stack`` | ``[]`` |
| ``projects[].year`` / ``type`` / ``role`` | **not parsed** | ``""`` |
| ``skillGroups`` | ``skills[]`` | single 「专业技能」group |
| ``awards`` | **not parsed** | ``[]`` |
| ``capabilities`` | latest + previous ``capability_snapshots`` | ``[]`` |
| ``spanLabel`` | ``experience_years`` | ``""`` |
| ``monthlyAdded`` | **not derivable** | ``0`` |

⚠️ ``monthlyAdded`` is deliberately ``0``, not a fabricated number. The
Figma prototype shows a nonzero value, but nothing in the backend can derive
"how many assets were added this month" until career assets have real write
endpoints (P4+). A wrong number here would be worse than a zero.

Capabilities are the one genuinely *derivable* block: every successful analysis
writes a ``capability_snapshots`` row, so ``score`` is the newest snapshot's
value and ``delta`` is its change vs the previous one — the same derivation
``growth.py`` uses for its events. Reusing it keeps the two features from
disagreeing about whether a score went up.
"""

from __future__ import annotations

from typing import Any, Optional

from app.careerly.schemas import (
    Award,
    Capability,
    CareerAssets,
    ProjectExperience,
    SkillDelta,
    SkillGroup,
    WorkExperience,
)

# Where all skills land when the parser gives no finer categorisation.
# ⚠️ The engine's ``ResumeProfile.skills`` is a flat list — it has no notion of
# "产品能力 / 技术能力". Inventing categories would be fabricated structure, so
# one honest bucket until the parser (or a user-editable asset table) does better.
DEFAULT_SKILL_CATEGORY = "专业技能"

_WORK_ID_PREFIX = "work"
_PROJECT_ID_PREFIX = "projasset"


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _as_list(value: Any) -> list:
    return list(value) if isinstance(value, (list, tuple)) else []


def _work_experiences(profile: dict) -> list[WorkExperience]:
    """One row per ``roles[]`` entry (title-only; company/period not parsed)."""
    roles = [_as_str(r).strip() for r in _as_list(profile.get("roles"))]
    rows: list[WorkExperience] = []
    seen: set[str] = set()
    for i, title in enumerate(roles):
        if not title or title in seen:
            continue
        seen.add(title)
        rows.append(
            WorkExperience(
                id=f"{_WORK_ID_PREFIX}_{i + 1}",
                period="",       # not parsed — see module docstring
                title=title,
                company="",      # not parsed
                description="",  # not parsed
                skills=[],       # per-role attribution not parsed
                projects=[],
            )
        )
    return rows


def _project_experiences(profile: dict) -> list[ProjectExperience]:
    """One row per ``projects[]`` entry."""
    rows: list[ProjectExperience] = []
    for i, raw in enumerate(_as_list(profile.get("projects"))):
        if not isinstance(raw, dict):
            continue
        name = _as_str(raw.get("name")).strip()
        if not name:
            continue
        highlights = [_as_str(h) for h in _as_list(raw.get("highlights")) if _as_str(h).strip()]
        rows.append(
            ProjectExperience(
                id=f"{_PROJECT_ID_PREFIX}_{i + 1}",
                name=name,
                year="",        # not parsed
                type="",        # not parsed
                role="",        # not parsed
                description=_as_str(raw.get("description")),
                outcome=highlights[0] if highlights else "",
                skills=[_as_str(s) for s in _as_list(raw.get("tech_stack")) if _as_str(s).strip()],
                jobImpact=[],   # requires per-job diffing, not available here
            )
        )
    return rows


def _skill_groups(profile: dict) -> list[SkillGroup]:
    skills = [_as_str(s).strip() for s in _as_list(profile.get("skills"))]
    skills = [s for s in dict.fromkeys(skills) if s]  # dedupe, keep order
    if not skills:
        return []
    return [SkillGroup(category=DEFAULT_SKILL_CATEGORY, skills=skills)]


def _capabilities(store: Any, user_id: int) -> list[Capability]:
    """Current capability profile with deltas.

    Thin wrapper over :meth:`CareerlyStore.list_capabilities` — the derivation
    lives on the store because ``home/summary`` needs the same numbers, and
    two copies would eventually disagree.
    """
    return [
        Capability(
            name=c["name"],
            score=c["score"],
            delta=c["delta"],
            previousScore=c["previousScore"],
        )
        for c in store.list_capabilities(user_id)
    ]


def _span_label(profile: dict) -> str:
    """Human-readable span. Only ``experience_years`` is available, so say that."""
    years = profile.get("experience_years")
    try:
        years_int = int(float(years))
    except (TypeError, ValueError):
        return ""
    if years_int <= 0:
        return ""
    return f"累计工作经验 {years_int} 年"


def _monthly_added(store: Any, user_id: int) -> int:
    """Deliberately ``0`` — see module docstring.

    Kept as a function (rather than a literal at the call site) so the intent
    is greppable: this is a *known gap*, not an oversight.
    """
    return 0


def build_career_assets(
    store: Any,
    user_id: int,
    profile: Optional[dict] = None,
) -> CareerAssets:
    """Assemble ``CareerAssets`` for one user.

    ``profile`` is the parsed resume profile; when ``None`` it is read from the
    store. Passing it in lets ``POST /analysis`` reuse the profile it just got
    from the engine instead of re-reading the row it wrote a moment ago.
    """
    if profile is None:
        saved = store.get_resume(user_id) or {}
        profile = saved.get("profile") or {}
    if not isinstance(profile, dict):
        profile = {}

    return CareerAssets(
        workExperiences=_work_experiences(profile),
        projects=_project_experiences(profile),
        skillGroups=_skill_groups(profile),
        awards=[],  # not parsed — honours/Prizes have no source yet
        capabilities=_capabilities(store, user_id),
        spanLabel=_span_label(profile),
        monthlyAdded=_monthly_added(store, user_id),
    )


# ``Award`` / ``SkillDelta`` are imported for the schema's sake and re-exported
# so the mapping table in the docstring stays checkable against real symbols.
__all__ = [
    "build_career_assets",
    "DEFAULT_SKILL_CATEGORY",
    "Award",
    "SkillDelta",
]
