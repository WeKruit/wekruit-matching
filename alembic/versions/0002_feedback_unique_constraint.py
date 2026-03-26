"""Add UNIQUE constraint on feedback(user_id, job_id) to prevent duplicates.

Revision ID: 0002
Revises: 0001
Create Date: 2026-03-26
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove any existing duplicates first (keep earliest)
    op.execute("""
        DELETE FROM feedback f1
        USING feedback f2
        WHERE f1.ctid > f2.ctid
          AND f1.user_id = f2.user_id
          AND f1.job_id = f2.job_id
    """)
    op.create_unique_constraint(
        "uq_feedback_user_job",
        "feedback",
        ["user_id", "job_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_feedback_user_job", "feedback", type_="unique")
