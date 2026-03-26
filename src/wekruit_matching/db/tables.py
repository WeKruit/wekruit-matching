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
    sa.Column("job_id", sa.String(64), primary_key=True, comment="SHA-256 of normalized company+title+url"),
    sa.Column("source_repo", sa.String(128), nullable=False, comment="SimplifyJobs repo slug"),
    # Raw scraped fields
    sa.Column("company_name", sa.Text, nullable=False),
    sa.Column("role_title", sa.Text, nullable=False),
    sa.Column("primary_url", sa.Text, nullable=True),
    sa.Column("location_raw", sa.Text, nullable=False, server_default=""),
    sa.Column("date_posted_raw", sa.Text, nullable=True),
    # Status tracking
    sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    # Content hash for enrichment gating (Phase 3)
    sa.Column("content_hash", sa.String(64), nullable=True, index=True),
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
    sa.Column("embedding_model", sa.String(64), nullable=True, comment="e.g. text-embedding-3-small"),
    sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
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
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
)

feedback_table = sa.Table(
    "feedback",
    metadata,
    sa.Column("feedback_id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("user_id", sa.Text, sa.ForeignKey("user_profiles.user_id"), nullable=False),
    sa.Column("job_id", sa.String(64), sa.ForeignKey("jobs.job_id"), nullable=False),
    sa.Column("reaction", sa.String(16), nullable=False, comment="like | dislike | applied"),
    sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    sa.Index("ix_feedback_user_job", "user_id", "job_id"),
)
