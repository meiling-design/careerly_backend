"""C4 capability scoring — LLM output validation & normalisation.

The LLM returns ``{"capabilities": [{"name", "yours", "target", "why", "actions"}]}``.
Nothing about that response is trusted until it passes :func:`normalize` —
wrong types, out-of-range scores or an empty/oversized list all degrade to
``None`` so the analysis falls back to the pre-C4 behaviour (never fails).
"""

from __future__ import annotations

from typing import Any, Optional

from app.utils.logger import logger

MIN_DIMENSIONS = 3
MAX_DIMENSIONS = 8


def _clamp_score(value: Any) -> Optional[int]:
    """Coerce to int 0-100; accept float/str digits, reject anything else."""
    try:
        n = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, n))


def normalize(raw: Any) -> Optional[list[dict[str, Any]]]:
    """Validate the LLM response into a clean capability list, else ``None``."""
    if not isinstance(raw, dict):
        return None
    caps = raw.get("capabilities")
    if not isinstance(caps, list) or not (MIN_DIMENSIONS <= len(caps) <= MAX_DIMENSIONS):
        logger.info("Careerly capability scoring rejected: %s dimensions", len(caps) if isinstance(caps, list) else "?")
        return None

    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in caps:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or name in seen:
            continue
        yours = _clamp_score(item.get("yours"))
        target = _clamp_score(item.get("target"))
        if yours is None or target is None:
            continue
        seen.add(name)
        why = str(item.get("why") or "").strip() or None
        actions = [str(a).strip() for a in (item.get("actions") or []) if str(a).strip()][:3]
        clean.append({"name": name, "yours": yours, "target": target, "why": why, "actions": actions})

    if len(clean) < MIN_DIMENSIONS:
        logger.info("Careerly capability scoring rejected: only %d valid dimensions", len(clean))
        return None
    return clean
