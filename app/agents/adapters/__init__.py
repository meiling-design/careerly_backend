"""Site adapters for universal job crawling.

Each adapter knows how to extract job postings from a specific
job platform's URL (or a category of URLs).

Usage:
    from app.agents.adapters import get_adapter_for_url, TencentAdapter

    adapter = get_adapter_for_url("https://careers.tencent.com/...")
    if adapter:
        jobs = await adapter.fetch_job(url)
"""

from typing import Optional

from app.agents.adapters.tencent import TencentAdapter
from app.agents.adapters.bytedance import ByteDanceAdapter
from app.agents.adapters.generic_html import GenericHTMLAdapter
from app.agents.adapters.generic_spa import GenericSPAAdapter
from app.services.llm_service import LLMService

# Order matters: more specific adapters first
_ADAPTERS = [
    TencentAdapter,
    ByteDanceAdapter,
    GenericSPAAdapter,   # Playwright fallback for SPAs
    GenericHTMLAdapter,  # requests+BS4 fallback
]

# Adapters that need an LLM service injected
_ADAPTERS_NEEDING_LLM = {GenericHTMLAdapter, GenericSPAAdapter}


def get_adapter_for_url(url: str, llm: Optional[LLMService] = None):
    """Return the first adapter that can handle the given URL.

    If *llm* is provided, it is injected into adapters that need it
    (e.g. GenericHTMLAdapter, GenericSPAAdapter) so they share the
    same LLM client instance.
    """
    for adapter_cls in _ADAPTERS:
        if adapter_cls.can_handle(url):
            if llm is not None and adapter_cls in _ADAPTERS_NEEDING_LLM:
                return adapter_cls(llm=llm)
            return adapter_cls()
    return None


def list_adapters() -> list[str]:
    """Return names of all registered adapters."""
    return [cls.__name__ for cls in _ADAPTERS]


__all__ = [
    "get_adapter_for_url",
    "list_adapters",
    "TencentAdapter",
    "ByteDanceAdapter",
    "GenericHTMLAdapter",
    "GenericSPAAdapter",
]
