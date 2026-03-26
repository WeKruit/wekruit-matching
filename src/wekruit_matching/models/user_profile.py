"""UserProfile data model.

Represents a user's matching preferences and accumulated feedback state.
Passed directly to get_matches() by the caller — no HTTP server involved.
"""
from enum import Enum
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class JobType(str, Enum):
    INTERN = "intern"
    NEW_GRAD = "new_grad"
    ANY = "any"


class CompanySizePreference(str, Enum):
    STARTUP = "startup"
    MIDSIZE = "midsize"
    LARGE = "large"
    ANY = "any"


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
    affinity_embedding: Optional[list[float]] = Field(
        None,
        description="1536-dim affinity vector (rolling weighted avg of liked job embeddings)",
    )
