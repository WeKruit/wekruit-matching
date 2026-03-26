"""Job data model.

Represents a single job listing as stored in the database.
job_id is our stable internal ID (SHA-256 hash of normalized company+title+url).
"""
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    ACTIVE = "active"
    INACTIVE = "inactive"  # Listing disappeared from source on a subsequent scrape


class Job(BaseModel):
    """A job listing scraped from SimplifyJobs."""

    # Identity
    job_id: str = Field(..., description="Stable SHA-256 hash of (normalized_company, normalized_title, primary_url)")
    source_repo: str = Field(..., description="SimplifyJobs repo slug, e.g. 'Summer2026-Internships'")

    # Raw fields from scrape
    company_name: str
    role_title: str
    primary_url: Optional[str] = None
    location_raw: str = Field("", description="Raw location string from README table cell")
    date_posted_raw: Optional[str] = None

    # Status
    status: JobStatus = JobStatus.ACTIVE
    first_seen_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow)

    # Content hash — SHA-256 of the enrichable text fields; used to gate re-enrichment
    content_hash: Optional[str] = None

    # LLM-enriched fields (populated in Phase 3)
    industry: Optional[str] = None
    company_size: Optional[str] = None  # "startup" | "midsize" | "large" | None
    required_skills: list[str] = Field(default_factory=list)
    sponsorship: Optional[bool] = None  # True=offers, False=no, None=unknown

    # Embedding fields (populated in Phase 4)
    # NOTE: embedding is NOT in this pydantic model — stored directly in DB as vector(1536)
    embedding_model: Optional[str] = None  # e.g. "text-embedding-3-small"
    enriched_at: Optional[datetime] = None
    embedded_at: Optional[datetime] = None

    @field_validator("content_hash")
    @classmethod
    def content_hash_format(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError("content_hash must be a 64-character lowercase hex string (SHA-256)")
        return v
