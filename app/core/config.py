"""Global configuration for the job matching system.

Loads config.yaml first, then .env overrides — so YAML sets defaults
and environment variables take precedence.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field

# 路径基准统一由 app.utils.paths 提供（见该模块文档：为什么不在 app/core/）。
# REPO_ROOT 随代码走；APP_DATA_DIR 可被 DATA_DIR 重定向到数据盘。
from app.utils.paths import APP_DATA_DIR, REPO_ROOT

load_dotenv()

CONFIG_YAML_PATH = REPO_ROOT / "config.yaml"


# ── YAML loader ──────────────────────────────────────────────────────────────


def _load_yaml_config(path: Path) -> dict[str, Any]:
    """Load config.yaml, return flattened dict or empty dict on failure."""
    try:
        import yaml
    except ImportError:
        return {}

    if not path.exists():
        return {}

    try:
        with open(path, encoding="utf-8") as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return data
    except Exception:
        return {}


def _yaml_val(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Safely traverse nested YAML dict: _yaml_val(data, 'llm', 'model')."""
    cur: Any = data
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return cur if cur is not None else default


# ── Config classes ────────────────────────────────────────────────────────────


class LLMConfig(BaseModel):
    """LLM provider configuration."""

    model: str = Field(default_factory=lambda: os.getenv("LLM_MODEL", "qwen3.5-plus"))
    api_key: str = Field(default_factory=lambda: os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY", ""))
    base_url: str = Field(default_factory=lambda: os.getenv("LLM_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL", ""))
    temperature: float = 0.1
    # qwen3.5-plus is a reasoning model: ~2.9k of the budget goes to
    # reasoning_tokens before any JSON is emitted, and a single resume parse
    # was measured at 3.5k completion tokens. 4096 left too little headroom
    # and longer resumes would truncate mid-JSON.
    max_tokens: int = 8192


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

    # ── LLM Hybrid Router ─────────────────────────────────────────────────
    use_llm_router: bool = True
    """Use LLM-enhanced routing. Falls back to rules when LLM unavailable."""

    llm_confidence_threshold: float = 0.6
    """Minimum confidence to accept LLM router decision. Below this → rule fallback."""

    enable_refine_crawl: bool = True
    """Allow router to suggest 'refine_crawl' (try different crawl strategy)."""

    enable_ask_user: bool = True
    """Allow router to suggest 'ask_user' (insufficient data, human needed)."""


class AppConfig(BaseModel):
    """Top-level application configuration."""

    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    crawl: CrawlConfig = Field(default_factory=CrawlConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    debug: bool = Field(default_factory=lambda: os.getenv("DEBUG", "false").lower() == "true")
    # 用 default_factory 而非模块级常量求值：config 单例可能在 DATA_DIR 尚未
    # 生效的时序下被构造，工厂形式可保证拿到 paths 模块的最终值。
    data_dir: Path = Field(default_factory=lambda: APP_DATA_DIR)
    output_dir: Path = Field(default_factory=lambda: APP_DATA_DIR / "outputs")

    model_config = {"frozen": False}

    @classmethod
    def from_yaml_and_env(cls) -> AppConfig:
        """Build AppConfig: config.yaml defaults + .env overrides.

        Strategy:
        1. Instantiate from environment variables first (the normal way).
        2. Overlay fields from config.yaml for any that are still at default.
        3. Environment variables (already baked into defaults) always win.
        """
        cfg = cls()
        yaml_data = _load_yaml_config(CONFIG_YAML_PATH)
        if not yaml_data:
            return cfg

        # ── LLM ──────────────────────────────────────────────────────────
        if not os.getenv("LLM_MODEL"):
            cfg.llm.model = _yaml_val(yaml_data, "llm", "model", default=cfg.llm.model)
        if not os.getenv("LLM_API_KEY") and not os.getenv("DASHSCOPE_API_KEY"):
            cfg.llm.api_key = _yaml_val(yaml_data, "llm", "api_key", default=cfg.llm.api_key)
        if not os.getenv("LLM_BASE_URL") and not os.getenv("DASHSCOPE_BASE_URL"):
            cfg.llm.base_url = _yaml_val(yaml_data, "llm", "base_url", default=cfg.llm.base_url)
        cfg.llm.temperature = _yaml_val(yaml_data, "llm", "temperature", default=cfg.llm.temperature)
        cfg.llm.max_tokens = _yaml_val(yaml_data, "llm", "max_tokens", default=cfg.llm.max_tokens)

        # ── Embedding ────────────────────────────────────────────────────
        if not os.getenv("EMBEDDING_PROVIDER"):
            cfg.embedding.provider = _yaml_val(yaml_data, "embedding", "provider", default=cfg.embedding.provider)
        if not os.getenv("EMBEDDING_MODEL"):
            cfg.embedding.model = _yaml_val(yaml_data, "embedding", "model", default=cfg.embedding.model)

        # ── Crawl ────────────────────────────────────────────────────────
        cfg.crawl.use_playwright = _yaml_val(yaml_data, "crawl", "use_playwright", default=cfg.crawl.use_playwright)
        cfg.crawl.timeout = _yaml_val(yaml_data, "crawl", "timeout", default=cfg.crawl.timeout)
        cfg.crawl.max_urls = _yaml_val(yaml_data, "crawl", "max_urls", default=cfg.crawl.max_urls)

        # ── Matching ─────────────────────────────────────────────────────
        cfg.matching.semantic_weight = _yaml_val(yaml_data, "matching", "semantic_weight", default=cfg.matching.semantic_weight)
        cfg.matching.llm_weight = _yaml_val(yaml_data, "matching", "llm_weight", default=cfg.matching.llm_weight)
        cfg.matching.overlap_weight = _yaml_val(yaml_data, "matching", "overlap_weight", default=cfg.matching.overlap_weight)
        cfg.matching.use_llm = _yaml_val(yaml_data, "matching", "use_llm", default=cfg.matching.use_llm)
        cfg.matching.use_embedding = _yaml_val(yaml_data, "matching", "use_embedding", default=cfg.matching.use_embedding)
        cfg.matching.two_stage = _yaml_val(yaml_data, "matching", "two_stage", default=cfg.matching.two_stage)
        cfg.matching.stage1_top_k = _yaml_val(yaml_data, "matching", "stage1_top_k", default=cfg.matching.stage1_top_k)
        cfg.matching.stage1_top_ratio = _yaml_val(yaml_data, "matching", "stage1_top_ratio", default=cfg.matching.stage1_top_ratio)
        cfg.matching.stage1_min_score = _yaml_val(yaml_data, "matching", "stage1_min_score", default=cfg.matching.stage1_min_score)

        # ── Routing ──────────────────────────────────────────────────────
        cfg.routing.resume_min_skills = _yaml_val(yaml_data, "routing", "resume_min_skills", default=cfg.routing.resume_min_skills)
        cfg.routing.jobs_min_count = _yaml_val(yaml_data, "routing", "jobs_min_count", default=cfg.routing.jobs_min_count)
        cfg.routing.max_recrawl = _yaml_val(yaml_data, "routing", "max_recrawl", default=cfg.routing.max_recrawl)

        # ── Debug ────────────────────────────────────────────────────────
        if not os.getenv("DEBUG"):
            cfg.debug = _yaml_val(yaml_data, "debug", default=cfg.debug)

        return cfg


# Global singleton (loaded from YAML + env)
config = AppConfig.from_yaml_and_env()
