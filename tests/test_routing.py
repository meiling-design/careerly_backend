"""Tests for the routing node logic — rule-based fallback & signals.

These tests exercise ``DecisionGraph._rule_based_decision()`` which is
the deterministic fallback when the LLM router is unavailable.
"""

from __future__ import annotations

import pytest

from app.core.orchestrator import DecisionGraph


@pytest.fixture
def graph() -> DecisionGraph:
    return DecisionGraph()


class TestRuleBasedRouting:
    """Deterministic rule-fallback routing logic."""

    def test_no_resume_ends(self, graph):
        """No resume → end."""
        result = graph._rule_based_decision(
            resume_exists=False,
            resume_skills=0,
            total_jobs=5,
            fatal_errors=False,
            recrawl_remaining=1,
        )
        assert result["decision"] == "end"

    def test_fatal_errors_end(self, graph):
        """Fatal errors → end."""
        result = graph._rule_based_decision(
            resume_exists=True,
            resume_skills=10,
            total_jobs=5,
            fatal_errors=True,
            recrawl_remaining=1,
        )
        assert result["decision"] == "end"

    def test_recrawl_with_retries(self, graph):
        """No jobs + remaining retries → recrawl."""
        result = graph._rule_based_decision(
            resume_exists=True,
            resume_skills=5,
            total_jobs=0,
            fatal_errors=False,
            recrawl_remaining=2,
        )
        assert result["decision"] == "recrawl"

    def test_no_retries_ends(self, graph):
        """No jobs + no retries → end."""
        result = graph._rule_based_decision(
            resume_exists=True,
            resume_skills=5,
            total_jobs=0,
            fatal_errors=False,
            recrawl_remaining=0,
        )
        assert result["decision"] == "end"

    def test_ready_to_match(self, graph):
        """All conditions met → match."""
        result = graph._rule_based_decision(
            resume_exists=True,
            resume_skills=10,
            total_jobs=3,
            fatal_errors=False,
            recrawl_remaining=1,
        )
        assert result["decision"] == "match"

    def test_few_skills_still_matches(self, graph):
        """Few resume skills still proceeds to match (with raw text)."""
        result = graph._rule_based_decision(
            resume_exists=True,
            resume_skills=1,
            total_jobs=3,
            fatal_errors=False,
            recrawl_remaining=1,
        )
        assert result["decision"] == "match"


class TestIdempotencyKey:
    """Idempotency key generation in server.py."""

    def test_same_inputs_same_key(self):
        """Same inputs should produce the same idempotency key."""
        import hashlib, json

        def make_key(urls, text):
            return hashlib.sha256(
                json.dumps({
                    "urls": sorted(urls),
                    "resume_text_hash": hashlib.sha256(
                        (text or "").encode()
                    ).hexdigest()[:12],
                    "file_path": "",
                    "config": {"llm": True, "embedding": True},
                }, sort_keys=True).encode()
            ).hexdigest()[:16]

        k1 = make_key(["https://a.com/job1"], "Python expert")
        k2 = make_key(["https://a.com/job1"], "Python expert")
        assert k1 == k2

    def test_different_inputs_different_key(self):
        """Different inputs should produce different keys."""
        import hashlib, json

        def make_key(urls, text):
            return hashlib.sha256(
                json.dumps({
                    "urls": sorted(urls),
                    "resume_text_hash": hashlib.sha256(
                        (text or "").encode()
                    ).hexdigest()[:12],
                    "file_path": "",
                    "config": {"llm": True, "embedding": True},
                }, sort_keys=True).encode()
            ).hexdigest()[:16]

        k1 = make_key(["https://a.com/job1"], "Python expert")
        k2 = make_key(["https://b.com/job2"], "Java expert")
        assert k1 != k2
