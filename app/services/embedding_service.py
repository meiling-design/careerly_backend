"""Embedding service for semantic similarity computation.

Supports:
- sentence-transformers (local, offline)
- OpenAI compatible embedding API (remote)
"""

from __future__ import annotations

import os
from typing import Any, Optional

import numpy as np
from dotenv import load_dotenv
from openai import OpenAI

from app.utils.helpers import chunk_text
from app.utils.logger import logger

load_dotenv()


class EmbeddingService:
    """Compute text embeddings for semantic matching."""

    def __init__(
        self,
        provider: str = "openai",
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        self.provider = provider

        if provider == "sentence-transformers":
            self._init_sentence_transformers(model)
        else:
            # Default: OpenAI-compatible API
            self.model = model or os.getenv("EMBEDDING_MODEL", "text-embedding-v3")
            self.dimensions: int = 1024

            base_url = base_url or os.getenv("EMBEDDING_BASE_URL") or os.getenv("DASHSCOPE_BASE_URL")
            api_key = api_key or os.getenv("EMBEDDING_API_KEY") or os.getenv("DASHSCOPE_API_KEY")

            if not api_key:
                raise ValueError("No API key for embeddings. Set EMBEDDING_API_KEY or DASHSCOPE_API_KEY.")

            self.client = OpenAI(api_key=api_key, base_url=base_url)
            logger.info("EmbeddingService (API) | model=%s", self.model)

    def _init_sentence_transformers(self, model: str | None) -> None:
        """Lazy-init sentence-transformers (locally)."""
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]

            model_name = model or "all-MiniLM-L6-v2"
            self._local_model = SentenceTransformer(model_name)
            self.dimensions = self._local_model.get_sentence_embedding_dimension() or 384
            self.provider = "sentence-transformers"
            logger.info("EmbeddingService (local) | model=%s | dim=%s", model_name, self.dimensions)
        except ImportError:
            logger.warning("sentence-transformers not installed, falling back to API mode")
            self.provider = "openai"
            # Re-init as API
            self.__init__(provider="openai", model=model)

    # ── Public interface ──────────────────────────────────────────────────

    def embed(self, text: str) -> list[float]:
        """Embed a single text string."""
        if self.provider == "sentence-transformers":
            return self._embed_local(text)
        return self._embed_api(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts."""
        if not texts:
            return []
        if self.provider == "sentence-transformers":
            return self._embed_many_local(texts)
        return self._embed_many_api(texts)

    def similarity(self, a: str, b: str) -> float:
        """Cosine similarity between two texts."""
        emb_a = np.array(self.embed(a))
        emb_b = np.array(self.embed(b))
        return self._cosine_sim(emb_a, emb_b)

    def similarity_matrix(
        self, candidates: list[str], target: str
    ) -> list[tuple[int, float]]:
        """Score each candidate against target. Returns [(idx, score), ...]."""
        target_emb = np.array(self.embed(target))
        cand_embs = [np.array(self.embed(c)) for c in candidates]
        scores = [(i, float(self._cosine_sim(c, target_emb))) for i, c in enumerate(cand_embs)]
        return sorted(scores, key=lambda x: x[1], reverse=True)

    def batch_similarity(
        self,
        target_embedding: list[float],
        candidate_embeddings: list[list[float]],
    ) -> list[float]:
        """Cosine similarity between one target and many candidates (pre-embedded).

        Args:
            target_embedding: Pre-computed target embedding vector.
            candidate_embeddings: List of pre-computed candidate vectors.

        Returns:
            List of similarity scores in same order as candidates.
        """
        target_arr = np.array(target_embedding)
        scores = []
        for cand in candidate_embeddings:
            if not cand:
                scores.append(0.0)
            else:
                scores.append(float(self._cosine_sim(target_arr, np.array(cand))))
        return scores

    def filter_top_k(
        self,
        target_embedding: list[float],
        candidate_embeddings: list[list[float]],
        k: int = 10,
        ratio: float = 0.2,
        min_score: float = 0.0,
    ) -> list[tuple[int, float]]:
        """Fast filter: return indices and scores of top-K candidates.

        Uses the larger of k or ratio * len(candidates) as the cutoff,
        then filters by min_score.

        Args:
            target_embedding: Resume embedding vector.
            candidate_embeddings: Job embedding vectors (same order as jobs).
            k: Absolute minimum to keep.
            ratio: Fraction to keep (whichever yields more).
            min_score: Minimum similarity score floor.

        Returns:
            List of (index, score) sorted descending.
        """
        scores = self.batch_similarity(target_embedding, candidate_embeddings)
        indexed = [(i, s) for i, s in enumerate(scores)]
        indexed.sort(key=lambda x: x[1], reverse=True)

        cutoff = max(k, int(len(indexed) * ratio))
        filtered = [(i, s) for i, s in indexed if s >= min_score]
        return filtered[:cutoff]

    # ── Internals ─────────────────────────────────────────────────────────

    def _embed_api(self, text: str) -> list[float]:
        """Embed via OpenAI-compatible API."""
        try:
            resp = self.client.embeddings.create(
                model=self.model,
                input=text,
                dimensions=self.dimensions,
            )
            return resp.data[0].embedding
        except Exception as e:
            logger.error("API embedding failed: %s", e)
            return []

    def _embed_many_api(self, texts: list[str]) -> list[list[float]]:
        """Batch embed via API."""
        try:
            resp = self.client.embeddings.create(
                model=self.model,
                input=texts,
                dimensions=self.dimensions,
            )
            # Sort by index to preserve order
            sorted_data = sorted(resp.data, key=lambda d: d.index)
            return [d.embedding for d in sorted_data]
        except Exception as e:
            logger.error("Batch API embedding failed: %s", e)
            return [[] for _ in texts]

    def _embed_local(self, text: str) -> list[float]:
        """Embed via local sentence-transformers model."""
        if not hasattr(self, "_local_model"):
            raise RuntimeError("Local model not initialized")
        emb = self._local_model.encode(text, show_progress_bar=False)
        return emb.tolist()

    def _embed_many_local(self, texts: list[str]) -> list[list[float]]:
        emb = self._local_model.encode(texts, show_progress_bar=False)  # type: ignore[union-attr]
        return emb.tolist()

    @staticmethod
    def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
        norm = np.linalg.norm(a) * np.linalg.norm(b)
        if norm == 0:
            return 0.0
        return float(np.dot(a, b) / norm)
