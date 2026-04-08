"""Add data quality score storage for ATS JD enrichment.

Revision ID: 0005
Revises: 0004
Create Date: 2026-03-31
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the ATS data quality score column."""
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS data_quality_score INTEGER"
    )


def downgrade() -> None:
    """Remove the ATS data quality score column."""
    op.drop_column("jobs", "data_quality_score")
