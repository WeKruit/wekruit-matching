"""Add dead + dead_confirmed_at flags for Postgres-side liveness tombstone.

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-09

P7-K (2026-05-09): Hybrid TTL + tombstone for dead-URL infinite loop.

Background
----------
Post v1.6 we run `paLivenessSweepDaily` (wekruit-pa Cloud Function) which
HEAD-checks every active matching-jobs URL and flags 404s as ``dead=true``
in Firestore. P7-J (2026-05-09) adds a TTL Cloud Function that hard-deletes
the Firestore doc 90 days after that flag.

The infinite-loop bug it closes:

  Day 1   scrape adds URL → Postgres (status=active) + Firestore (active)
  Day 5   liveness sweep 404 → Firestore dead=true (Postgres unchanged)
  Day 95  P7-J TTL deletes Firestore doc
  Day 96  scrape source still lists URL → ON CONFLICT resets Postgres
            to status=active → re-syncs to Firestore as fresh doc
  Day 100 liveness sweep 404 again → loop forever, burns Firecrawl + LLM
            tokens for nothing.

Solution: defense-in-depth. Postgres also remembers ``dead`` so the
scraper's UPSERT can short-circuit on already-dead URLs (Stage 0 pulls
the dead set from Firestore at pipeline start, scraper.upsert respects
the flag on conflict). Single ON CONFLICT statement; no SELECT-then-
UPSERT race.

Why a separate column from ``permanent_404`` (added in 0006)
------------------------------------------------------------
``permanent_404`` is set by Stage 2b ATS JD enrichment when the JD-fetch
HTTP call returns 404. ``dead`` is set by the post-enrichment liveness
sweep that HEAD-checks the *apply* URL after the listing has been live.
The two signals overlap in practice but are produced by different
subsystems on different schedules, and conflating them would leak Stage
2b transient failures into the long-term tombstone. Keep them
orthogonal — Stage 2b is a recoverable-vs-permanent JD-fetch outcome,
``dead`` is a confirmed-dead URL tombstone.

Schema additions (both NULL-safe, idempotent)
---------------------------------------------
* ``dead BOOLEAN DEFAULT FALSE`` — true once liveness sweep confirms 404
* ``dead_confirmed_at TIMESTAMP`` — when the flag was first set; powers
  the 30-day skip window + 90-day single-retry path in upsert.py

Backfill is *not* done in this migration — the Firestore source of truth
is in a different system (wekruit-pa) and pulling it from inside alembic
would require a Firestore SDK + creds in the migration runtime. The
companion daily-pipeline Stage 0 (``firestore_dead_backfill`` in
pipeline/daily.py) handles backfill on every run, idempotently.
"""
from alembic import op


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add dead + dead_confirmed_at columns (NULL-safe, idempotent)."""
    # IF NOT EXISTS keeps this idempotent for production-safe re-runs
    # (matches the pattern from 0004/0006).
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dead BOOLEAN DEFAULT FALSE"
    )
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS dead_confirmed_at TIMESTAMP"
    )


def downgrade() -> None:
    """Drop dead + dead_confirmed_at columns."""
    op.drop_column("jobs", "dead_confirmed_at")
    op.drop_column("jobs", "dead")
