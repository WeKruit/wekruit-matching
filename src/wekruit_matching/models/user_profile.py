"""UserProfile data model.

Represents a user's matching preferences and accumulated feedback state.
Passed directly to get_matches() by the caller — no HTTP server involved.
"""
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class JobType(StrEnum):
    INTERN = "intern"
    NEW_GRAD = "new_grad"
    ANY = "any"


class CompanySizePreference(StrEnum):
    STARTUP = "startup"
    MIDSIZE = "midsize"
    LARGE = "large"
    ANY = "any"


class DerivedExperience(BaseModel):
    """Read-only PA derivedExperience model consumed by the matching ranker."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    version: str = "v1"
    years_total: float | None = Field(None, alias="yearsTotal")
    years_per_skill: dict[str, float] = Field(default_factory=dict, alias="yearsPerSkill")
    skill_recency: dict[str, str] = Field(default_factory=dict, alias="skillRecency")
    title_trajectory: list[str] = Field(default_factory=list, alias="titleTrajectory")
    seniority_current: str = Field("unknown", alias="seniorityCurrent")
    responsibility_current: str = Field("unknown", alias="responsibilityCurrent")
    industry_history: dict[str, float] = Field(default_factory=dict, alias="industryHistory")
    unverified_skills: list[str] = Field(default_factory=list, alias="unverifiedSkills")
    computed_at: str | None = Field(None, alias="computedAt")


class UserProfile(BaseModel):
    """A user's job-matching preferences and accumulated feedback state."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    user_id: str = Field(..., description="Caller-provided opaque user identifier")

    # Explicit preferences (aliases match VALET's MatchRequest field names)
    skills: list[str] = Field(default_factory=list, description="User's self-reported skills")
    preferred_job_type: JobType = Field(JobType.ANY, alias="job_type")
    preferred_locations: list[str] = Field(
        default_factory=list,
        alias="location_prefs",
        description="Preferred location strings (normalized at match time)",
    )
    requires_sponsorship: bool = Field(False, alias="sponsorship_needed")
    preferred_company_size: CompanySizePreference = Field(
        CompanySizePreference.ANY, alias="company_size_pref"
    )
    preferred_industries: list[str] = Field(default_factory=list, alias="industries")

    # Feedback state (updated by feedback handler in Phase 7)
    liked_companies: list[str] = Field(default_factory=list)
    disliked_companies: list[str] = Field(default_factory=list)

    # Affinity embedding (rolling average of liked job embeddings — updated in Phase 7)
    # Stored as a plain Python list because pydantic doesn't know pgvector types
    affinity_embedding: list[float] | None = Field(None, max_length=1536)

    # PA global candidate profile fields. These are optional and read-only from
    # matching's perspective; PA's extractor trigger owns writes.
    total_years_experience: float | None = Field(None, alias="totalYearsExperience")
    derived_experience: DerivedExperience | None = Field(None, alias="derivedExperience")
    derived_experience_version: str | None = Field(None, alias="derivedExperienceVersion")
    derived_experience_content_hash: str | None = Field(
        None, alias="derivedExperienceContentHash"
    )
