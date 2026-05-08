"""Add permanent_404 boolean flag for Stage 2b JD fetch outcome distinction.

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-08

P7-F (2026-05-08): Stage 2b ATS JD enrichment previously gated only on
``jd_fetch_attempted_at IS NULL``, so any failed fetch (Firecrawl outage,
Workday 5xx, transient connection error) permanently locked the job out
of the queue. Mirroring the P7-E staleness pattern, the gating fix in
``run_jd_enrichment.py`` adds a 7-day re-attempt window for *recoverable*
failures. To keep the queue clean of *permanent* failures (HTTP 404 / dead
URL / employer pulled the listing), this column flags those rows so the
SELECT predicate excludes them entirely.

Why a boolean column rather than a 3-value enum (``recoverable_fail`` /
``permanent_fail`` / ``success``):

- Cheaper migration: single additive column, defaults FALSE for the
  ~3.3K already-failed rows so they're treated as recoverable on the
  next run (which is what we want — they failed during the Firecrawl
  outage and should retry now that Firecrawl is back).
- ``success`` is already implicit in the row state (``jd_fetch_source``
  is non-NULL and not ``'failed'``, or ``job_description`` is populated).
  An enum would duplicate that signal.
- IS NOT TRUE / COALESCE(..., FALSE) handles NULLs naturally for any rows
  that may not be backfilled.
"""
from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the permanent_404 boolean flag (NULL-safe, default FALSE)."""
    # ADD COLUMN IF NOT EXISTS keeps this idempotent for production-safe
    # re-runs (matches the pattern from 0004_add_jd_fetch_tracking_columns).
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS permanent_404 BOOLEAN DEFAULT FALSE"
    )


def downgrade() -> None:
    """Remove the permanent_404 flag."""
    op.drop_column("jobs", "permanent_404")
