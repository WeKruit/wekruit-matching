"""Initial schema: jobs, user_profiles, feedback tables with pgvector HNSW index.

Revision ID: 0001
Revises: (none)
Create Date: 2026-03-25
"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ensure pgvector extension is installed
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- jobs table ---
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("source_repo", sa.String(128), nullable=False),
        sa.Column("company_name", sa.Text, nullable=False),
        sa.Column("role_title", sa.Text, nullable=False),
        sa.Column("primary_url", sa.Text, nullable=True),
        sa.Column("location_raw", sa.Text, nullable=False, server_default=""),
        sa.Column("date_posted_raw", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("industry", sa.Text, nullable=True),
        sa.Column("company_size", sa.String(32), nullable=True),
        sa.Column("required_skills", sa.ARRAY(sa.Text), nullable=False,
                  server_default=sa.text("ARRAY[]::text[]")),
        sa.Column("sponsorship", sa.Boolean, nullable=True),
        # vector(1536) for OpenAI text-embedding-3-small
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column("embedding_model", sa.String(64), nullable=True),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Index on content_hash for enrichment-gate lookups
    op.create_index("ix_jobs_content_hash", "jobs", ["content_hash"])

    # HNSW index on embedding column with vector_cosine_ops
    # CRITICAL: Must use vector_cosine_ops to match <=> (cosine distance) queries.
    # Using the wrong operator class causes the planner to ignore the index and
    # fall back to sequential scan — producing correct results at O(N) latency.
    # ef_construction=64, m=16 are the pgvector recommended defaults for this scale.
    op.execute("""
        CREATE INDEX ix_jobs_embedding_hnsw
        ON jobs
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)

    # --- user_profiles table ---
    op.create_table(
        "user_profiles",
        sa.Column("user_id", sa.Text, primary_key=True),
        sa.Column("skills", sa.ARRAY(sa.Text), nullable=False,
                  server_default=sa.text("ARRAY[]::text[]")),
        sa.Column("preferred_job_type", sa.String(16), nullable=False, server_default="any"),
        sa.Column("preferred_locations", sa.ARRAY(sa.Text), nullable=False,
                  server_default=sa.text("ARRAY[]::text[]")),
        sa.Column("requires_sponsorship", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("preferred_company_size", sa.String(16), nullable=False, server_default="any"),
        sa.Column("preferred_industries", sa.ARRAY(sa.Text), nullable=False,
                  server_default=sa.text("ARRAY[]::text[]")),
        sa.Column("liked_companies", sa.ARRAY(sa.Text), nullable=False,
                  server_default=sa.text("ARRAY[]::text[]")),
        sa.Column("disliked_companies", sa.ARRAY(sa.Text), nullable=False,
                  server_default=sa.text("ARRAY[]::text[]")),
        sa.Column("affinity_embedding", Vector(1536), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )

    # --- feedback table ---
    op.create_table(
        "feedback",
        sa.Column("feedback_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Text, sa.ForeignKey("user_profiles.user_id"), nullable=False),
        sa.Column("job_id", sa.String(64), sa.ForeignKey("jobs.job_id"), nullable=False),
        sa.Column("reaction", sa.String(16), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
    )
    op.create_index("ix_feedback_user_job", "feedback", ["user_id", "job_id"])


def downgrade() -> None:
    op.drop_table("feedback")
    op.drop_table("user_profiles")
    op.drop_index("ix_jobs_embedding_hnsw", table_name="jobs")
    op.drop_index("ix_jobs_content_hash", table_name="jobs")
    op.drop_table("jobs")
