"""Job posting data models."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class JobSource(str, Enum):
    """Source type for job postings."""
    MANUAL = "manual"
    CRAWL = "crawl"
    API = "api"
    FILE = "file"


class JobPosting(BaseModel):
    """A single job posting from any source."""

    title: str = Field(..., description="Job title")
    location: Optional[str] = Field(None, description="Job location")
    department: Optional[str] = Field(None, description="Department or team")
    company: Optional[str] = Field(None, description="Company name")
    description: str = Field(..., description="Full job description text")
    url: Optional[str] = Field(None, description="Source URL of the job posting")

    # Enhanced fields (populated by LLM parsing)
    required_skills: list[str] = Field(default_factory=list, description="Extracted required skills")
    preferred_skills: list[str] = Field(default_factory=list, description="Preferred/nice-to-have skills")
    required_experience: Optional[int] = Field(None, description="Minimum years of experience required")
    responsibilities: list[str] = Field(default_factory=list, description="Key job responsibilities")
    soft_skills: list[str] = Field(default_factory=list, description="Soft skills mentioned")

    # Metadata
    source: JobSource = Field(default=JobSource.CRAWL, description="How this job was obtained")
    crawled_at: Optional[str] = Field(None, description="ISO timestamp when crawled")
    raw_text: Optional[str] = Field(None, description="Raw HTML/text before cleaning")

    model_config = {"frozen": False, "extra": "ignore"}

    def summary(self, max_desc: int = 200) -> str:
        """Short human-readable summary."""
        desc_short = self.description[:max_desc].replace("\n", " ") + ("..." if len(self.description) > max_desc else "")
        skills = ", ".join(self.required_skills[:5])
        extras = " + ..." if len(self.required_skills) > 5 else ""
        return (
            f"[{self.title}] @ {self.company or 'Unknown'} | {self.location or 'Remote'}\n"
            f"  Skills: {skills}{extras}\n"
            f"  Description: {desc_short}"
        )


class JobList(BaseModel):
    """Collection of job postings from one crawl/interaction."""

    jobs: list[JobPosting] = Field(default_factory=list)
    total_count: int = 0
    session_id: Optional[str] = Field(None, description="UUID for this crawl session")

    def add(self, job: JobPosting) -> None:
        self.jobs.append(job)
        self.total_count = len(self.jobs)

    def deduplicate(self, key: str = "url") -> None:
        seen = set()
        unique: list[JobPosting] = []
        for j in self.jobs:
            val = getattr(j, key, None) or j.title
            if val not in seen:
                seen.add(val)
                unique.append(j)
        self.jobs = unique
        self.total_count = len(self.jobs)
