"""Agent modules for the job matching pipeline."""

from app.agents.crawl_agent import CrawlAgent
from app.agents.resume_agent import ResumeAgent
from app.agents.matching_agent import MatchingAgent
from app.agents.ranking_agent import RankingAgent

# Re-export adapters for convenience
from app.agents.adapters import (
    ByteDanceAdapter,
    GenericHTMLAdapter,
    GenericSPAAdapter,
    TencentAdapter,
    get_adapter_for_url,
    list_adapters,
)

__all__ = [
    "CrawlAgent",
    "ResumeAgent",
    "MatchingAgent",
    "RankingAgent",
    "TencentAdapter",
    "ByteDanceAdapter",
    "GenericHTMLAdapter",
    "GenericSPAAdapter",
    "get_adapter_for_url",
    "list_adapters",
]
