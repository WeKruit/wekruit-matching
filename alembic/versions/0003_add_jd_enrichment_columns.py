"""Add JD storage and enrichment columns to jobs table.

New columns for storing full job descriptions and LLM-extracted structured
fields used by the enrichment pipeline.

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-26
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use ADD COLUMN IF NOT EXISTS for production safety — idempotent if
    # columns were partially added by a previous interrupted run.
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS job_description TEXT")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS core_responsibilities TEXT[] DEFAULT '{}'")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS salary_range TEXT")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS seniority_level TEXT")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS benefits TEXT[] DEFAULT '{}'")
    op.execute("ALTER TABLE jobs ADD COLUMN IF NOT EXISTS qualifications TEXT[] DEFAULT '{}'")


def downgrade() -> None:
    op.drop_column("jobs", "qualifications")
    op.drop_column("jobs", "benefits")
    op.drop_column("jobs", "seniority_level")
    op.drop_column("jobs", "salary_range")
    op.drop_column("jobs", "core_responsibilities")
    op.drop_column("jobs", "job_description")
