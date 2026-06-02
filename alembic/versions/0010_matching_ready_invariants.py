"""Matching-ready invariants: CHECK constraints that make STAMP_WITHOUT_VERIFY
states unrepresentable (reliability audit 2026-06-01, rank 2).

Why this migration exists
-------------------------
The recurring "matchable dropped overnight" incidents were all one class: a
writer stamps a done-flag (enriched_at, a real jd_fetch_source, embedded_at)
without the data the flag certifies, and Postgres permitted the inconsistent
state because there were ZERO CHECK constraints. The producer fixes (ranks
4/5/14) stop NEW violations at the source; these constraints make the corruption
states structurally impossible so the Nth future sibling writer cannot
re-introduce them.

Four invariants (JD-aware so the genuine empty-at-source floor — a row with no
usable JD to extract skills from — is NOT rejected):

  c1 ck_enriched_requires_skills_or_no_jd:
     enriched_at IS NULL OR cardinality(required_skills) > 0
       OR job_description IS NULL OR length(job_description) < 200
     -- enriched_at may certify a row only when it has skills, UNLESS there was
        no usable JD to extract them from (legit floor).
  c2 ck_embedded_requires_vector:
     embedded_at IS NULL OR embedding IS NOT NULL
  c3 ck_vector_requires_embedded_stamp:
     embedding IS NULL OR embedded_at IS NOT NULL
  c4 ck_jd_source_requires_usable_jd:
     jd_fetch_source IS NULL
       OR jd_fetch_source IN ('failed','skip_no_url','closed_at_source')
       OR (job_description IS NOT NULL AND length(job_description) >= 200)

Pre-flight backfill (REQUIRED — else ADD CONSTRAINT fails on existing dirty
rows). Verified before writing this migration: ACTIVE rows have 0 violations of
all four; the violators are inactive/historical (c1 ~1,426, c4 ~74). The
backfill repairs them to the invariant (NULL the bad enriched_at; rewrite the
thin-JD source to 'failed') so the constraints can be added cleanly. c2/c3 had
0 violations table-wide.

Idempotent: backfill UPDATEs are naturally idempotent; constraints use a guard
so re-running does not error.
"""
from __future__ import annotations

from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


_CONSTRAINTS = {
    "ck_enriched_requires_skills_or_no_jd": (
        "enriched_at IS NULL "
        "OR cardinality(required_skills) > 0 "
        "OR job_description IS NULL "
        "OR length(job_description) < 200"
    ),
    "ck_embedded_requires_vector": (
        "embedded_at IS NULL OR embedding IS NOT NULL"
    ),
    "ck_vector_requires_embedded_stamp": (
        "embedding IS NULL OR embedded_at IS NOT NULL"
    ),
    "ck_jd_source_requires_usable_jd": (
        "jd_fetch_source IS NULL "
        "OR jd_fetch_source IN ('failed','skip_no_url','closed_at_source') "
        "OR (job_description IS NOT NULL AND length(job_description) >= 200)"
    ),
}


def upgrade() -> None:
    # --- Pre-flight backfill (repair existing violators) --------------------
    # c1: a row marked enriched with empty skills BUT a usable JD is a genuine
    # extraction miss — clear enriched_at so the gap-fill re-enricher retries it.
    op.execute(
        """
        UPDATE jobs SET enriched_at = NULL
        WHERE enriched_at IS NOT NULL
          AND cardinality(required_skills) = 0
          AND job_description IS NOT NULL
          AND length(job_description) >= 200
        """
    )
    # c4: a real ATS source name on a thin/empty JD is an unusable fetch —
    # rewrite to 'failed' so Stage 2b re-admits it (matches the rank-5 runtime
    # behaviour).
    op.execute(
        """
        UPDATE jobs SET jd_fetch_source = 'failed'
        WHERE jd_fetch_source IS NOT NULL
          AND jd_fetch_source NOT IN ('failed','skip_no_url','closed_at_source')
          AND (job_description IS NULL OR length(job_description) < 200)
        """
    )
    # c2/c3: embedded_at without a vector (or vice-versa) — repair the stamp.
    op.execute(
        "UPDATE jobs SET embedded_at = NULL WHERE embedded_at IS NOT NULL AND embedding IS NULL"
    )

    # --- Add the CHECK constraints (guarded so re-run is a no-op) -----------
    for name, predicate in _CONSTRAINTS.items():
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = '{name}'
                ) THEN
                    ALTER TABLE jobs ADD CONSTRAINT {name} CHECK ({predicate});
                END IF;
            END $$;
            """
        )


def downgrade() -> None:
    for name in _CONSTRAINTS:
        op.execute(f"ALTER TABLE jobs DROP CONSTRAINT IF EXISTS {name}")
