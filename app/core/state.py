"""LangGraph state definitions — the typed state that flows through the decision graph.

Upgraded with:
- Embedding cache (job_id → vector)
- Routing decision tracking
- 2-stage matching fields (stage1_scores, top_k_indices)
- Recrawl counter
"""

from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Optional

from app.models.job import JobList
from app.models.match import MatchReport, MatchResult
from app.models.resume import ResumeProfile


@dataclass
class AgentStatus:
    """Status tracking for each node in the graph."""

    name: str
    status: str = "pending"  # pending | running | completed | failed | skipped
    error: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class GraphState:
    """The complete state flowing through the LangGraph decision graph.

    Each node reads from and writes to this state.
    Nodes return a dict of updates to merge into the state.
    """

    # ── Input ────────────────────────────────────────────────────────────
    job_urls: list[str] = field(default_factory=list)
    resume_file_path: Optional[str] = None
    resume_text: Optional[str] = None
    session_id: Optional[str] = None

    # ── Crawl output ─────────────────────────────────────────────────────
    crawler_output: Optional[JobList] = None

    # ─── Normalized output ───────────────────────────────────────────────
    normalized_jobs: Optional[JobList] = None
    """Jobs after LLM-based field enrichment (skills, experience, etc.)."""

    # ── Resume output ────────────────────────────────────────────────────
    resume_output: Optional[ResumeProfile] = None
    resume_raw_text: Optional[str] = None
    """Resume raw text used for embedding and LLM prompting."""

    # ── Embedding cache (keyed by job_index or "resume") ─────────────────
    embedding_cache: dict[str, list[float]] = field(default_factory=dict)
    """Cache for computed embeddings to avoid re-API calls.
       Key format: "resume" or "job_{index}".
    """

    # ── Routing decision ─────────────────────────────────────────────────
    routing_decision: str = ""
    """What the Routing Node decided: 'match' | 'recrawl' | 'skip' | 'end'."""

    routing_reason: str = ""
    """Why the routing decision was made."""

    # ── 2-stage matching: Stage 1 (embedding filter) ─────────────────────
    stage1_scores: list[dict] = field(default_factory=list)
    """[{job_index, title, semantic_score}, ...] — all jobs scored by embedding."""

    top_k_indices: list[int] = field(default_factory=list)
    """Indices of top-K jobs that passed the embedding filter (Stage 1)."""

    stage1_top_k: int = 10
    """How many candidates pass the embedding filter to Stage 2 LLM rerank."""

    stage1_top_ratio: float = 0.2
    """Ratio of top candidates to pass (whichever yields more candidates)."""

    # ── 2-stage matching: Stage 2 (LLM rerank) ──────────────────────────
    match_results: list[MatchResult] = field(default_factory=list)
    """Full match results after Stage 2 LLM rerank."""

    # ── Report ───────────────────────────────────────────────────────────
    report: Optional[MatchReport] = None

    # ── Recrawl tracking ─────────────────────────────────────────────────
    recrawl_count: int = 0
    """How many times we've gone through the recrawl loop."""

    max_recrawl: int = 1
    """Max allowed recrawl iterations."""

    # ── Control flow ─────────────────────────────────────────────────────
    errors: list[str] = field(default_factory=list)
    statuses: dict[str, AgentStatus] = field(default_factory=dict)
    progress_log: Annotated[list[str], operator.add] = field(default_factory=list)

    # ── Config flags ─────────────────────────────────────────────────────
    use_llm: bool = True
    use_embedding: bool = True
    debug: bool = False

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0

    def log(self, message: str) -> None:
        """Add a progress log entry."""
        self.progress_log.append(message)
