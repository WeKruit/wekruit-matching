"""UserProfile data model.

Represents a user's matching preferences and accumulated feedback state.
Passed directly to get_matches() by the caller — no HTTP server involved.
"""
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


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

    user_id: str = Field(..., description="Caller-provided opaque user identifier")

    # Explicit preferences
    skills: list[str] = Field(default_factory=list, description="User's self-reported skills")
    preferred_job_type: JobType = JobType.ANY
    preferred_locations: list[str] = Field(
        default_factory=list,
        description="Preferred location strings (normalized at match time)",
    )
    requires_sponsorship: bool = False
    preferred_company_size: CompanySizePreference = CompanySizePreference.ANY
    preferred_industries: list[str] = Field(default_factory=list)

    # Feedback state (updated by feedback handler in Phase 7)
    liked_companies: list[str] = Field(default_factory=list)
    disliked_companies: list[str] = Field(default_factory=list)

    # Affinity embedding (rolling average of liked job embeddings — updated in Phase 7)
    # Stored as a plain Python list because pydantic doesn't know pgvector types
    affinity_embedding: Optional[list[float]] = Field(
        None,
        description="1536-dim affinity vector (rolling weighted avg of liked job embeddings)",
    )
