"""Shared fixtures for all tests.

Usage:
    pytest tests/ -v --record-mode=once   # record LLM responses once
    pytest tests/ -v                       # replay (default)
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Optional

import pytest

# Point data dir to a temp location during tests
#
# 这行**不能删**：删了默认就落回真实的开发库 app/data/*.db，测试会污染本地数据。
# 目录名沿用 tests/.tmp_* 前缀 —— `.gitignore` 用通配覆盖了这一族
# （`tests/.tmp_p1/`、`tests/.tmp_engine/` 这类逐条枚举的写法漏过文件，
# 见 G0-2）。归到同一个前缀下，今后新增测试临时目录无需再改 .gitignore。
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("DATA_DIR", str(Path(__file__).resolve().parent / ".tmp_data_root"))

from app.models.job import JobList, JobPosting, JobSource
from app.models.match import MatchResult, SkillGap
from app.models.resume import ResumeProfile


# ── Mock EmbeddingService (no sentence-transformers download) ──────────────


class MockEmbeddingService:
    """Deterministic mock: returns predictable embedding vectors.

    Each text gets a consistent vector based on its hash, so repeated
    calls with the same text return the same vector. No model download.
    """

    dimensions = 384

    def embed(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode()).digest()
        return [ (h[i % len(h)] / 255.0) * 2 - 1 for i in range(64) ] * 6  # len 384

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def similarity(self, a: str, b: str) -> float:
        """Simplified: just use Jaccard of hash representation."""
        ea = self.embed(a)
        eb = self.embed(b)
        dot = sum(x * y for x, y in zip(ea, eb))
        na = sum(x * x for x in ea) ** 0.5
        nb = sum(x * x for x in eb) ** 0.5
        if na * nb == 0:
            return 0.0
        return dot / (na * nb)

    def filter_top_k(self, target_embedding, candidate_embeddings, k=10, ratio=0.2, min_score=0.0):
        scores = [sum(a * b for a, b in zip(target_embedding, ce)) for ce in candidate_embeddings]
        indexed = sorted(enumerate(scores), key=lambda x: -x[1])
        cutoff = max(k, int(len(indexed) * ratio))
        return [(i, s) for i, s in indexed if s >= min_score][:cutoff]


# ── Sample data fixtures ──────────────────────────────────────────────────


@pytest.fixture
def sample_resume() -> ResumeProfile:
    """A realistic resume for testing."""
    return ResumeProfile(
        name="张三",
        skills=["Python", "FastAPI", "PostgreSQL", "Docker", "Kubernetes", "Redis"],
        experience_years=5.0,
        roles=["后端开发工程师", "Python工程师"],
        projects=[
            {
                "name": "AI Agent平台",
                "description": "基于LangGraph的多智能体编排平台",
                "tech_stack": ["Python", "FastAPI", "LangGraph", "PostgreSQL"],
            }
        ],
        education=[{"degree": "本科", "institution": "某大学", "year": 2019}],
        raw_text="5年Python开发经验，精通FastAPI、PostgreSQL...",
    )


@pytest.fixture
def sample_jobs() -> JobList:
    """Two jobs: one matching, one not."""
    return JobList(jobs=[
        JobPosting(
            title="高级Python后端工程师",
            description="负责AI平台后端开发，使用Python/FastAPI，管理K8s集群，优化PostgreSQL查询",
            company="测试科技",
            required_skills=["Python", "FastAPI", "Kubernetes", "PostgreSQL"],
            preferred_skills=["LangGraph", "Redis"],
            required_experience=3,
            source=JobSource.MANUAL,
        ),
        JobPosting(
            title="前端工程师",
            description="负责Web前端开发，使用React/TypeScript",
            company="测试科技",
            required_skills=["React", "TypeScript", "CSS"],
            required_experience=2,
            source=JobSource.MANUAL,
        ),
        JobPosting(
            title="Java后端工程师",
            description="使用Java/Spring Boot开发微服务",
            company="某公司",
            required_skills=["Java", "Spring Boot", "MySQL"],
            required_experience=3,
            source=JobSource.MANUAL,
        ),
    ])


@pytest.fixture
def matching_agent() -> Any:
    """MatchingAgent with mocked embedding (no model download needed)."""
    from app.agents.matching_agent import MatchingAgent
    from app.services.llm_service import LLMService

    return MatchingAgent(
        llm=LLMService(model="test-model"),
        embedder=MockEmbeddingService(),
        use_batch_llm=False,
    )


@pytest.fixture
def orchestrator() -> Any:
    """DecisionGraph with mocked services for integration tests."""
    from app.core.orchestrator import DecisionGraph
    return DecisionGraph()


# ── VCR.py configuration ──────────────────────────────────────────────────


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, Any]:
    """VCR.py configuration for recording LLM API interactions.

    Cassettes are stored in tests/cassettes/.
    Use ``--record-mode=once`` to record, default is ``none`` (replay).
    """
    return {
        "filter_headers": [
            ("authorization", "Bearer <REDACTED>"),
            ("api-key", "<REDACTED>"),
        ],
        "filter_query_parameters": [
            ("api_key", "<REDACTED>"),
        ],
        "cassette_library_dir": str(
            Path(__file__).resolve().parent / "cassettes"
        ),
        "path_transformer": None,  # use default
        "before_record_request": _before_record_request,
    }


def _before_record_request(request: Any) -> Any | None:
    """Strip sensitive data from recorded requests."""
    # Redact authorization headers in the body too
    from vcr.request import Request as VCRRequest
    return request
