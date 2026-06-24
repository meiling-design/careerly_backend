"""Resume / candidate profile data models."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Project(BaseModel):
    """A project listed on a resume."""

    name: str = Field(..., description="Project name")
    description: Optional[str] = Field(None, description="Brief project description")
    tech_stack: list[str] = Field(default_factory=list, description="Technologies used")
    highlights: list[str] = Field(default_factory=list, description="Key achievements or results")


class Education(BaseModel):
    """Education entry."""

    degree: str = Field(..., description="Degree name, e.g. 'B.S. Computer Science'")
    institution: str = Field(..., description="School or university name")
    year: Optional[int] = Field(None, description="Graduation year")


class ResumeProfile(BaseModel):
    """Parsed resume data — the candidate's skill profile."""

    # Raw candidate info
    name: Optional[str] = Field(None, description="Candidate name")
    email: Optional[str] = Field(None, description="Email address")
    phone: Optional[str] = Field(None, description="Phone number")

    # Structured extraction
    skills: list[str] = Field(default_factory=list, description="All identified skills")
    experience_years: Optional[float] = Field(None, description="Total years of professional experience")
    roles: list[str] = Field(default_factory=list, description="Previous or target job titles")
    projects: list[Project] = Field(default_factory=list, description="Notable projects")
    education: list[Education] = Field(default_factory=list, description="Education background")
    certifications: list[str] = Field(default_factory=list, description="Professional certifications")

    # Raw text fallback
    raw_text: Optional[str] = Field(None, description="Full resume text for LLM fallback")

    model_config = {"frozen": False}

    def skill_keywords(self) -> list[str]:
        """Return lowercase, stripped skill keywords for matching."""
        return [s.strip().lower() for s in self.skills if s.strip()]

    def summary(self) -> str:
        """Short human-readable summary."""
        roles_str = ", ".join(self.roles[:3]) if self.roles else "N/A"
        skills_str = ", ".join(self.skills[:8])
        extras = " ..." if len(self.skills) > 8 else ""
        return (
            f"👤 {self.name or 'Unknown'} | {self.experience_years or '?'}yr exp\n"
            f"  Roles: {roles_str}\n"
            f"  Skills: {skills_str}{extras}\n"
            f"  Projects: {len(self.projects)}"
        )
