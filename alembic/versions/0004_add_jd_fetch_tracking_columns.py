"""Add JD fetch tracking columns and partial index for pipeline stage 2 (ATS parsing). Revision ID: 0004. Revises: 0003. Create Date: 2026-03-31.

Four new columns record where JD content was sourced, when the last fetch was
attempted, a hash of the parsed ATS content (separate from content_hash which
hashes the scraped listing row), and the resolved canonical apply URL at the
employer ATS. The partial index covers the pipeline query that finds jobs
needing a JD fetch: WHERE status = 'active' AND job_description IS NULL.
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use ADD COLUMN IF NOT EXISTS for production safety — idempotent if
    # columns were partially added by a previous interrupted run.
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS jd_fetch_source TEXT")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS jd_fetch_attempted_at TIMESTAMPTZ")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ats_content_hash TEXT")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ats_apply_url TEXT")

    # Partial index for the pipeline query pattern: find jobs that need JD fetch.
    # Covers: WHERE status = 'active' AND job_description IS NULL
    #          ORDER BY jd_fetch_attempted_at NULLS FIRST LIMIT 500
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_jobs_jd_fetch_pending
        ON jobs (status, jd_fetch_attempted_at)
        WHERE job_description IS NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_jobs_jd_fetch_pending")
    op.drop_column("jobs", "ats_apply_url")
    op.drop_column("jobs", "ats_content_hash")
    op.drop_column("jobs", "jd_fetch_attempted_at")
    op.drop_column("jobs", "jd_fetch_source")
