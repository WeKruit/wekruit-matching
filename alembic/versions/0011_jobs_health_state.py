"""jobs_health_state: rolling baseline for the blocking pre-sync gate
(reliability audit 2026-06-01, Gate-4 / IL-5).

Why this migration exists
-------------------------
``health_gate.assert_pre_sync_ready`` is a BLOCKING gate that runs AFTER embed
and BEFORE the Firestore sync. Besides the absolute ==0 matching-ready
invariants, it enforces a relative floor: matchable_corpus must not have dropped
below the last KNOWN-GOOD value. That last value has to survive across runs and
across processes (the post-run observability gate persists its full metric set
to a /tmp JSON file, but a blocking pre-sync floor must be durable and shared,
not run-local). This table is that durable rolling baseline.

Schema
------
    jobs_health_state(
        metric     text PRIMARY KEY,   -- e.g. 'matchable'
        value      bigint NOT NULL,    -- the baseline count
        updated_at timestamptz NOT NULL DEFAULT now()
    )

One row per tracked metric; the gate UPSERTs the current matchable count after a
clean pass. Idempotent: CREATE/DROP are guarded so a re-run is a no-op and the
CI upgrade/downgrade round-trip passes.
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs_health_state (
            metric     text PRIMARY KEY,
            value      bigint NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS jobs_health_state")
