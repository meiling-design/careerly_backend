"""Distributed cache service for embeddings and LLM results.

Provides a unified key-value cache with two backends:
  - In-memory dict (always available, fast, survives within-process)
  - Redis (optional, survives restarts, shared across workers)

The caller controls backend priority via ``prefer_redis``.

Usage:
    cache = CacheService(redis_url="redis://localhost:6379/0")
    await cache.set("embed:abc123", [0.1, 0.2, ...])
    value = await cache.get("embed:abc123")
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from app.utils.logger import logger


class CacheService:
    """Dual-backend cache: in-memory dict + optional Redis.

    Gracefully degrades when Redis is unavailable.
    All public methods accept a *ttl* (seconds, default 3600).
    """

    def __init__(
        self,
        redis_url: str | None = None,
        default_ttl: int = 3600,
        max_memory_items: int = 5000,
    ):
        self.default_ttl = default_ttl
        self._redis_url = redis_url or os.getenv("REDIS_URL", "")
        self._redis: Any = None
        self._redis_ok = False

        # In-memory fallback: {key: (value, expires_at)}
        self._memory: dict[str, tuple[Any, float]] = {}
        self._max_memory_items = max_memory_items

        if self._redis_url:
            try:
                import redis.asyncio as aioredis  # type: ignore[import-untyped]
                self._redis = aioredis.from_url(
                    self._redis_url,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                    decode_responses=False,
                )
                logger.info("CacheService: Redis configured | url=%s", self._redis_url)
            except Exception as e:
                logger.warning("CacheService: Redis unavailable, using memory-only: %s", e)
                self._redis = None
        else:
            logger.info("CacheService: no REDIS_URL, using in-memory cache only")

    # ── Lifecycle ──────────────────────────────────────────────────────────

    async def close(self) -> None:
        """Close Redis connection (if any)."""
        if self._redis is not None:
            try:
                await self._redis.close()
            except Exception:
                pass

    # ── Core operations ────────────────────────────────────────────────────

    async def get(self, key: str) -> Any | None:
        """Get value by key. Returns None on miss."""
        # 1. Try memory first (fastest)
        if key in self._memory:
            value, expires = self._memory[key]
            if expires > time.time():
                return value
            del self._memory[key]

        # 2. Try Redis
        if self._redis is not None:
            try:
                raw = await self._redis.get(key)
                if raw is not None:
                    value = json.loads(raw)
                    # Populate memory as well (hot cache)
                    self._memory[key] = (value, time.time() + 300)
                    return value
            except Exception as e:
                logger.debug("CacheService: Redis GET failed: %s", e)

        return None

    async def set(
        self,
        key: str,
        value: Any,
        ttl: int | None = None,
    ) -> None:
        """Set value with TTL (seconds). Defaults to instance default_ttl."""
        ttl = ttl if ttl is not None else self.default_ttl
        expires = time.time() + ttl

        # Memory
        if len(self._memory) >= self._max_memory_items:
            self._evict_one()
        self._memory[key] = (value, expires)

        # Redis (fire-and-forget)
        if self._redis is not None:
            try:
                raw = json.dumps(value, ensure_ascii=False, default=str)
                await self._redis.setex(key, ttl, raw)
            except Exception as e:
                logger.debug("CacheService: Redis SET failed: %s", e)

    async def delete(self, key: str) -> None:
        """Delete a key from both backends."""
        self._memory.pop(key, None)
        if self._redis is not None:
            try:
                await self._redis.delete(key)
            except Exception:
                pass

    async def exists(self, key: str) -> bool:
        """Check if key exists in either backend."""
        if key in self._memory:
            return True
        if self._redis is not None:
            try:
                return bool(await self._redis.exists(key))
            except Exception:
                pass
        return False

    async def flush_all(self) -> None:
        """Clear all cached entries (both backends)."""
        self._memory.clear()
        if self._redis is not None:
            try:
                await self._redis.flushdb()
            except Exception:
                pass

    # ── Batch operations ───────────────────────────────────────────────────

    async def get_many(self, keys: list[str]) -> dict[str, Any]:
        """Bulk get. Returns {key: value} for hits."""
        results: dict[str, Any] = {}
        now = time.time()

        # Memory hits
        remaining: list[str] = []
        for k in keys:
            if k in self._memory:
                v, exp = self._memory[k]
                if exp > now:
                    results[k] = v
                    continue
                del self._memory[k]
            remaining.append(k)

        # Redis bulk fetch for remaining
        if remaining and self._redis is not None:
            try:
                raw_list = await self._redis.mget(remaining)
                for k, raw in zip(remaining, raw_list):
                    if raw is not None:
                        value = json.loads(raw)
                        results[k] = value
                        self._memory[k] = (value, now + 300)
            except Exception as e:
                logger.debug("CacheService: Redis MGET failed: %s", e)

        return results

    async def set_many(
        self,
        mapping: dict[str, Any],
        ttl: int | None = None,
    ) -> None:
        """Bulk set with shared TTL."""
        ttl = ttl if ttl is not None else self.default_ttl
        expires = time.time() + ttl

        for k, v in mapping.items():
            if len(self._memory) >= self._max_memory_items:
                self._evict_one()
            self._memory[k] = (v, expires)

        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                for k, v in mapping.items():
                    raw = json.dumps(v, ensure_ascii=False, default=str)
                    pipe.setex(k, ttl, raw)
                await pipe.execute()
            except Exception as e:
                logger.debug("CacheService: Redis pipeline set failed: %s", e)

    # ── Embedding-specific helpers ─────────────────────────────────────────

    @staticmethod
    def embed_key(prefix: str, text_hash: str) -> str:
        """Build a cache key for an embedding vector.

        Args:
            prefix: e.g. "resume" or "job"
            text_hash: SHA-256 hex digest of the source text.

        Returns:
            Cache key like ``embed:resume:abcd1234``.
        """
        return f"embed:{prefix}:{text_hash[:16]}"

    @staticmethod
    def llm_key(prefix: str, text_hash: str) -> str:
        """Build a cache key for a pre-computed LLM result."""
        return f"llm:{prefix}:{text_hash[:16]}"

    # ── Internals ──────────────────────────────────────────────────────────

    def _evict_one(self) -> None:
        """Evict the oldest entry from memory cache."""
        if not self._memory:
            return
        oldest = min(self._memory.keys(), key=lambda k: self._memory[k][1])
        del self._memory[oldest]


# Singleton for shared use across the app
_cache_instance: CacheService | None = None


def get_cache() -> CacheService:
    """Get or create the shared CacheService singleton."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = CacheService()
    return _cache_instance
