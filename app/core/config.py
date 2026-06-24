"""Global configuration for the job matching system."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent.parent


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    model: str = Field(default_factory=lambda: os.getenv("LLM_MODEL", "qwen3.5-plus"))
    api_key: str = Field(default_factory=lambda: os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY", ""))
    base_url: str = Field(default_factory=lambda: os.getenv("LLM_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL", ""))
    temperature: float = 0.1
    max_tokens: int = 4096


class EmbeddingConfig(BaseModel):
    """Embedding service configuration."""

    provider: str = Field(default="openai")  # "openai" or "sentence-transformers"
    model: str = Field(default_factory=lambda: os.getenv("EMBEDDING_MODEL", "text-embedding-v3"))
    api_key: str = Field(default_factory=lambda: os.getenv("EMBEDDING_API_KEY") or os.getenv("DASHSCOPE_API_KEY", ""))
    base_url: str = Field(default_factory=lambda: os.getenv("EMBEDDING_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL", ""))


class CrawlConfig(BaseModel):
    """Crawler configuration."""

    use_playwright: bool = True
    timeout: int = 30
    max_urls: int = 20


class MatchingConfig(BaseModel):
    """Matching weights configuration."""

    semantic_weight: float = 0.3
    llm_weight: float = 0.4
    overlap_weight: float = 0.3
    use_llm: bool = True
    use_embedding: bool = True

    # ── 2-stage ranking ──────────────────────────────────────────────────
    two_stage: bool = True
    """Enable 2-stage matching: embedding filter → LLM rerank."""

    stage1_top_k: int = 10
    """Minimum candidates passing embedding filter to Stage 2."""

    stage1_top_ratio: float = 0.2
    """Top ratio passing to Stage 2 (whichever yields more)."""

    stage1_min_score: float = 0.3
    """Embedding score floor — jobs below this are excluded from Stage 2."""


class RoutingConfig(BaseModel):
    """Routing Node configuration."""

    resume_min_skills: int = 3
    """Minimum skills on resume to avoid recrawl prompt."""

    jobs_min_count: int = 1
    """Minimum jobs required to proceed to matching."""

    max_recrawl: int = 1
    """Maximum recrawl iterations."""


class AppConfig(BaseModel):
    """Top-level application configuration."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    debug: bool = Field(default_factory=lambda: os.getenv("DEBUG", "false").lower() == "true")
    data_dir: Path = ROOT_DIR / "app" / "data"
    output_dir: Path = ROOT_DIR / "app" / "data" / "outputs"

    model_config = {"frozen": False}


# Global singleton
config = AppConfig()
