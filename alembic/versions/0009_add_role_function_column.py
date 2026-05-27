"""Add role_function + sources columns to jobs

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-27

Why this migration exists
-------------------------
The scraper upsert (`src/wekruit_matching/scraper/upsert.py`) + the
direct-API scrapers (greenhouse_direct.py, lever_direct.py, ashby_direct.py)
persist two columns to `jobs` that were added on the macmini's Postgres
manually but never committed as alembic migrations:

  - `role_function TEXT[]`  (commit 9431eab 2026-05-09)
  - `sources       TEXT[]`  (Job.sources field on src/wekruit_matching/
                             models/job.py line 41 — used for cross-source
                             dedup attribution)

Result: any fresh PG (Adam's laptop fallback, GH Actions runner, fresh
docker compose) crashes Stage 1 with `column "<col>" of relation "jobs"
does not exist`. This migration is the catch-up that covers BOTH columns
in one revision (idempotent via `IF NOT EXISTS`) so a half-applied state
is impossible.
"""
from __future__ import annotations

from alembic import op


# Alembic identifiers.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add `role_function TEXT[]` + `sources TEXT[]` to jobs (idempotent)."""
    # TEXT[] mirrors the existing `required_skills` ARRAY column shape that
    # upsert.py already uses with the same psycopg array binding.
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS role_function TEXT[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS sources TEXT[] NOT NULL DEFAULT '{}'"
    )
    # GIN indexes for `array-contains-any`-style filters. Cheap to add /
    # drop; the matching service relies on these for query selectivity.
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_role_function
        ON jobs USING GIN (role_function)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_sources
        ON jobs USING GIN (sources)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_jobs_sources")
    op.execute("DROP INDEX IF EXISTS idx_jobs_role_function")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS sources")
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS role_function")
