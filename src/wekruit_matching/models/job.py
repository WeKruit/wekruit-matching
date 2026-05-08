"""Job data model.

Represents a single job listing as stored in the database.
job_id is our stable internal ID (SHA-256 hash of normalized company+title+url).
"""
import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


def _utcnow() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(UTC)


class JobStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"  # Listing disappeared from source on a subsequent scrape


class Job(BaseModel):
    """A job listing scraped from SimplifyJobs."""

    # Identity
    job_id: str = Field(
        ...,
        description="Stable SHA-256 hash of (normalized_company, normalized_title, primary_url)",
    )
    source_repo: str = Field(
        ...,
        description="SimplifyJobs repo slug, e.g. 'Summer2026-Internships'",
    )

    # Phase 63 (v1.7) — multi-source attribution. Carries the source-list
    # alongside the legacy `source_repo` (which still drives stale-marking
    # and per-source upsert grouping). On dedup hit across sources, this
    # array is merged so downstream Firebase sync can write
    # `matching-jobs.{id}.sources: ['jobright','linkedin', ...]`.
    sources: list[str] = Field(
        default_factory=list,
        description="Phase 63: per-source attribution (e.g. ['jobright', 'linkedin']).",
    )

    # Raw fields from scrape
    company_name: str
    role_title: str
    primary_url: str | None = None
    location_raw: str = Field("", description="Raw location string from README table cell")
    date_posted_raw: str | None = None

    # Status
    status: JobStatus = JobStatus.ACTIVE
    first_seen_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime = Field(default_factory=_utcnow)

    # Content hash — SHA-256 of the enrichable text fields; used to gate re-enrichment
    content_hash: str | None = None
    job_description: str | None = None

    # LLM-enriched fields (populated in Phase 3)
    industry: str | None = None
    company_size: str | None = None  # "startup" | "midsize" | "large" | None
    required_skills: list[str] = Field(default_factory=list)
    sponsorship: bool | None = None  # True=offers, False=no, None=unknown

    # Phase 52 / Phase 63 — careerStage seniority inferred from role title
    # (intern | entry_level | junior | mid_level | senior | staff | principal |
    # manager | director | vp | c_level). Optional in this model — may be
    # populated either by the scraper (Phase 63 LinkedIn / Wellfound) or
    # downstream enrichment.
    seniority_level: str | None = None

    # P10-audit fix (2026-05-06) — jobright-canonical role-function bucket(s).
    # 17 enum values matching wekruit-pa
    # `packages/shared-tags/src/canonical/role-function.ts`. Used by V16
    # `queryMatchingJobs` as an array-contains-any HARD FILTER, so wrong/
    # missing values silently drop the job from match. Computed at scrape
    # time from `role_title` via
    # `wekruit_matching.scraper.title_inference.infer_role_function()`.
    # Empty list = unmatched (V16 will drop).
    role_function: list[str] = Field(
        default_factory=list,
        description="Jobright role-function tokens (D1, hard-filter axis).",
    )

    # Embedding fields (populated in Phase 4)
    # NOTE: embedding is NOT in this pydantic model — stored directly in DB as vector(1536)
    embedding_model: str | None = None  # e.g. "text-embedding-3-small"
    enriched_at: datetime | None = None
    embedded_at: datetime | None = None

    @field_validator("content_hash")
    @classmethod
    def content_hash_format(cls, v: str | None) -> str | None:
        if v is not None and not re.fullmatch(r"[0-9a-f]{64}", v):
            raise ValueError(
                "content_hash must be a 64-character lowercase hex string (SHA-256)"
            )
        return v
