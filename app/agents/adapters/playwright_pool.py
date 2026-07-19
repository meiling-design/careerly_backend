"""Shared Playwright resource management.

All adapters should acquire the semaphore before launching a browser
instance. This prevents N concurrent crawl requests from spawning
N×300 MB browser instances simultaneously.

Usage:
    from app.agents.adapters.playwright_pool import acquire_playwright, release_playwright
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from app.utils.logger import logger

# Max concurrent Playwright browser instances (env override)
_MAX_PLAYWRIGHT = int(os.getenv("MAX_PLAYWRIGHT_INSTANCES", "2"))

# Shared semaphore — all Playwright browsers across all adapters
# compete for the same pool.
_playwright_semaphore = asyncio.Semaphore(_MAX_PLAYWRIGHT)
_playwright_lock = asyncio.Lock()
_playwright_available: int = _MAX_PLAYWRIGHT


async def acquire_playwright(adapter_name: str = "unknown") -> bool:
    """Acquire a Playwright slot. Blocks until a slot is free.

    Paired with :func:`release_playwright` in a ``try/finally``.
    """
    global _playwright_available
    logger.debug("PlaywrightPool: waiting (adapter=%s)", adapter_name)
    await _playwright_semaphore.acquire()
    _playwright_available -= 1
    logger.debug("PlaywrightPool: acquired (adapter=%s, remaining=%d)",
                 adapter_name, _playwright_available)
    return True


def release_playwright(adapter_name: str = "unknown") -> None:
    """Release a Playwright slot (call in ``finally``)."""
    global _playwright_available
    _playwright_semaphore.release()
    _playwright_available += 1
    logger.debug("PlaywrightPool: released (adapter=%s, available=%d)",
                 adapter_name, _playwright_available)


async def playwright_fetch(
    url: str,
    adapter_name: str,
    fetcher: Any,
    timeout_ms: int = 30000,
) -> str | None:
    """Acquire a Playwright slot, run *fetcher*, release on completion.

    *fetcher* is an async callable that receives an async Playwright
    ``Browser`` and returns the rendered HTML (or ``None``).

    Returns:
        The rendered HTML string, or ``None`` on failure.
    """
    if not await acquire_playwright(adapter_name):
        return None

    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--single-process",          # reduce memory per instance
                ],
            )
            try:
                html = await fetcher(browser)
                return html
            finally:
                try:
                    await browser.close()
                except Exception as e:
                    logger.warning("PlaywrightPool: browser.close() failed: %s", e)
    except ImportError:
        logger.warning("PlaywrightPool: playwright not installed")
        return None
    except Exception as e:
        logger.error("PlaywrightPool: fetch failed for %s: %s", url, e)
        return None
    finally:
        release_playwright(adapter_name)
