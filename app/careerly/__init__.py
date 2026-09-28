"""Careerly backend namespace.

Contract-aligned API layered on top of the existing job-matching engine.

⚠️ The JSON shapes in ``schemas.py`` are **dictated by the Flutter app's
``fromJson`` / ``toJson``**, not by backend preference.  See
``docs/careerly/BACKEND_API_CONTRACT.md`` and the Flutter sources under
``G:/ai_agent/careerly/lib/data/models/``.

Layout::

    schemas.py   Pydantic models mirroring the Dart JSON contract
    store.py     SQLite persistence for the Careerly tables
    engine.py    bridge to DecisionGraph + MatchResult -> JobMatch mapping
    router.py    /api/v1/analysis | /growth | /resume ... endpoints
"""
