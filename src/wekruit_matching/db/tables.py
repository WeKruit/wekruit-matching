"""SQLAlchemy 2.x table definitions.

These definitions are used by alembic for migration generation (autogenerate)
and can be used for type-safe queries in the application.

CRITICAL: The embedding column uses pgvector's Vector type with 1536 dimensions.
The HNSW index on this column MUST use vector_cosine_ops — this is enforced
in the alembic migration, not here (SQLAlchemy doesn't know pgvector index ops).
"""
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy import MetaData

# Single MetaData instance — imported by alembic/env.py as target_metadata
metadata = MetaData()

jobs_table = sa.Table(
    "jobs",
    metadata,
    # Identity
    sa.Column(
        "job_id",
        sa.String(64),
        primary_key=True,
        comment="SHA-256 of normalized company+title+url",
    ),
    sa.Column(
        "source_repo",
        sa.String(128),
        nullable=False,
        comment="SimplifyJobs repo slug",
    ),
    # Raw scraped fields
    sa.Column("company_name", sa.Text, nullable=False),
    sa.Column("role_title", sa.Text, nullable=False),
    sa.Column("primary_url", sa.Text, nullable=True),
    sa.Column("location_raw", sa.Text, nullable=False, server_default=""),
    sa.Column("date_posted_raw", sa.Text, nullable=True),
    # Status tracking
    sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    sa.Column(
        "first_seen_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    sa.Column(
        "last_seen_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    # Content hash for enrichment gating (Phase 3)
    sa.Column("content_hash", sa.String(64), nullable=True, index=True),
    # Full JD text and LLM-extracted structured fields (Phase 3 enrichment)
    sa.Column(
        "job_description",
        sa.Text,
        nullable=True,
        comment="Full job description text",
    ),
    sa.Column(
        "core_responsibilities",
        sa.ARRAY(sa.Text),
        nullable=False,
        server_default="{}",
        comment="LLM-extracted list of core responsibilities",
    ),
    sa.Column(
        "salary_range",
        sa.Text,
        nullable=True,
        comment="Salary info string",
    ),
    sa.Column(
        "seniority_level",
        sa.Text,
        nullable=True,
        comment="entry/mid/senior",
    ),
    sa.Column(
        "benefits",
        sa.ARRAY(sa.Text),
        nullable=False,
        server_default="{}",
        comment="LLM-extracted list of benefits",
    ),
    sa.Column(
        "qualifications",
        sa.ARRAY(sa.Text),
        nullable=False,
        server_default="{}",
        comment="LLM-extracted list of qualifications",
    ),
    # JD fetch tracking (Phase 14)
    sa.Column(
        "jd_fetch_source",
        sa.Text,
        nullable=True,
        comment="greenhouse | lever | ashby | workday | firecrawl | failed",
    ),
    sa.Column(
        "jd_fetch_attempted_at",
        sa.DateTime(timezone=True),
        nullable=True,
        comment="Last time JD fetch was attempted",
    ),
    sa.Column(
        "ats_content_hash",
        sa.String(64),
        nullable=True,
        index=True,
        comment="SHA-256 of normalized ATS-sourced JD text",
    ),
    sa.Column(
        "data_quality_score",
        sa.Integer,
        nullable=True,
        comment="0-100 quality score for ATS-sourced JD completeness",
    ),
    # LLM-enriched fields (Phase 3)
    sa.Column("industry", sa.Text, nullable=True),
    sa.Column("company_size", sa.String(32), nullable=True),
    sa.Column("required_skills", sa.ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("sponsorship", sa.Boolean, nullable=True),
    # Embedding fields (Phase 4)
    # vector(1536) — OpenAI text-embedding-3-small output dimension
    # NOTE: HNSW index with vector_cosine_ops is created in the alembic migration via op.execute(),
    # NOT here. SQLAlchemy cannot express pgvector index operator classes declaratively.
    sa.Column("embedding", Vector(1536), nullable=True),
    sa.Column(
        "embedding_model",
        sa.String(64),
        nullable=True,
        comment="e.g. text-embedding-3-small",
    ),
    sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    # Matching-ready invariants (reliability audit 2026-06-01, rank 2). These
    # mirror the CHECK constraints added in alembic 0010 so the SQLAlchemy schema
    # and the live DB agree. They make the STAMP_WITHOUT_VERIFY corruption states
    # unrepresentable: a done-flag can never certify data that isn't there.
    sa.CheckConstraint(
        "enriched_at IS NULL "
        "OR cardinality(required_skills) > 0 "
        "OR job_description IS NULL "
        "OR length(job_description) < 200",
        name="ck_enriched_requires_skills_or_no_jd",
    ),
    sa.CheckConstraint(
        "embedded_at IS NULL OR embedding IS NOT NULL",
        name="ck_embedded_requires_vector",
    ),
    sa.CheckConstraint(
        "embedding IS NULL OR embedded_at IS NOT NULL",
        name="ck_vector_requires_embedded_stamp",
    ),
    sa.CheckConstraint(
        "jd_fetch_source IS NULL "
        "OR jd_fetch_source IN ('failed','skip_no_url','closed_at_source') "
        "OR (job_description IS NOT NULL AND length(job_description) >= 200)",
        name="ck_jd_source_requires_usable_jd",
    ),
)

user_profiles_table = sa.Table(
    "user_profiles",
    metadata,
    sa.Column("user_id", sa.Text, primary_key=True),
    sa.Column("skills", sa.ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("preferred_job_type", sa.String(16), nullable=False, server_default="any"),
    sa.Column("preferred_locations", sa.ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("requires_sponsorship", sa.Boolean, nullable=False, server_default="false"),
    sa.Column("preferred_company_size", sa.String(16), nullable=False, server_default="any"),
    sa.Column("preferred_industries", sa.ARRAY(sa.Text), nullable=False, server_default="{}"),
    # Feedback-derived fields (populated in Phase 7)
    sa.Column("liked_companies", sa.ARRAY(sa.Text), nullable=False, server_default="{}"),
    sa.Column("disliked_companies", sa.ARRAY(sa.Text), nullable=False, server_default="{}"),
    # Affinity embedding (rolling average of liked job embeddings — Phase 7)
    sa.Column("affinity_embedding", Vector(1536), nullable=True),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
)

feedback_table = sa.Table(
    "feedback",
    metadata,
    sa.Column("feedback_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("user_profiles.user_id"), nullable=False),
    sa.Column("job_id", sa.String(64), sa.ForeignKey("jobs.job_id"), nullable=False),
    sa.Column(
        "reaction",
        sa.String(16),
        nullable=False,
        comment="like | dislike | applied",
    ),
    sa.Column(
        "recorded_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
    sa.Index("ix_feedback_user_job", "user_id", "job_id"),
)

# Rolling baseline for the BLOCKING pre-sync data-quality gate (reliability
# audit 2026-06-01, Gate-4 / IL-5). One row per tracked metric (e.g.
# "matchable"); health_gate.assert_pre_sync_ready UPSERTs the current matchable
# count after a clean pass and reads it back as the relative floor on the next
# run. Mirrors alembic 0011 so the SQLAlchemy schema and the live DB agree.
jobs_health_state_table = sa.Table(
    "jobs_health_state",
    metadata,
    sa.Column(
        "metric",
        sa.Text,
        primary_key=True,
        comment="metric name, e.g. 'matchable'",
    ),
    sa.Column(
        "value",
        sa.BigInteger,
        nullable=False,
        comment="last known-good baseline count for this metric",
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("NOW()"),
    ),
)
