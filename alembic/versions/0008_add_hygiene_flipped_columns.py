"""Add hygiene_flipped + audit columns for PA hygiene write-back.

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-20

Matching-quality launch blocker (2026-05-20): close the hygiene-sync race.

The bug it closes
-----------------
1. PA ``paJobPoolHygiene`` Cloud Function flips a Firestore matching-jobs
   doc to ``status='inactive'`` (e.g. for ``yc_synthetic_title`` /
   ``jd_zombie`` / ``apply_url_not_job_page``).
2. Postgres-side ``jobs.status`` is unchanged — PA never writes Postgres.
3. Next daily scrape: ``scraper/upsert.py`` ON CONFLICT clause has
   ``status = 'active'`` unconditional. The freshly-flipped row gets
   reset to active.
4. Next ``job_sync.sync_jobs_to_firebase`` SELECTs ``WHERE status =
   'active'`` → writes the doc back to Firestore as active.
5. Hygiene flip undone within 24h.

Schema additions
----------------
* ``hygiene_flipped BOOLEAN NOT NULL DEFAULT FALSE`` — PA sets TRUE via
  the new ``POST /jobs/hygiene-flip`` endpoint when the doc fails any
  hygiene predicate. Upsert ON CONFLICT preserves ``status`` when this
  flag is TRUE.
* ``hygiene_flipped_at TIMESTAMP`` — first flip time; preserved across
  idempotent re-calls via COALESCE.
* ``hygiene_flip_reason TEXT`` — first reason recorded; preserved
  across idempotent re-calls. Free-form so any new hygiene predicate
  in wekruit-pa works without a schema bump.

Idempotency contract
--------------------
Every column is ADD COLUMN IF NOT EXISTS so this migration can be
re-applied safely. The PA endpoint's UPDATE uses COALESCE on the
timestamp + reason so duplicate flips don't overwrite the audit trail.
"""
from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add hygiene_flipped + audit columns (idempotent)."""
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hygiene_flipped BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hygiene_flipped_at TIMESTAMP"
    )
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hygiene_flip_reason TEXT"
    )
    # Partial index — most rows are FALSE, only the flipped tail needs lookups.
    # IF NOT EXISTS keeps re-application safe.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_hygiene_flipped
        ON jobs (hygiene_flipped_at DESC)
        WHERE hygiene_flipped IS TRUE
        """
    )


def downgrade() -> None:
    """Drop hygiene_flipped + audit columns."""
    op.execute("DROP INDEX IF EXISTS idx_jobs_hygiene_flipped")
    op.drop_column("jobs", "hygiene_flip_reason")
    op.drop_column("jobs", "hygiene_flipped_at")
    op.drop_column("jobs", "hygiene_flipped")
