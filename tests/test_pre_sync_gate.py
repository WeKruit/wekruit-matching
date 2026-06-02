"""Integration tests for the BLOCKING pre-sync data-quality gate
(health_gate.assert_pre_sync_ready) — reliability audit Gate-4 / IL-5.

These require a live Postgres test DB and use the WS-A ``pg_conn`` fixture
(function-scoped, rolled back, prod-guarded by WEKRUIT_TEST_DB=1 + a *_test DB
name). Marked @integration so the default suite deselects them.

The gate must:
  * RAISE PreSyncGateError when an absolute matching-ready invariant is violated
    (here: stamp-without-verify — enriched_at set + usable JD + zero skills), so
    the caller skips sync and corrupt data never reaches Firestore.
  * PASS on a clean corpus and UPSERT the current matchable count into
    jobs_health_state as the rolling baseline.
  * Enforce the relative floor once a baseline exists (a matchable drop below it
    blocks), and SKIP the relative floor on the first run (no baseline) so a
    healthy first run is never blocked.

NOTE on the stamp-without-verify INSERT: alembic 0010 added a CHECK constraint
(ck_enriched_requires_skills_or_no_jd) that makes that corruption shape
unrepresentable. To test the gate's *detection* of a legacy dirty row (one that
predates the constraint, or arrived via a path that bypassed it), we DROP that
constraint inside the rolled-back test transaction before inserting. The
rollback restores it; production is untouched.
"""

from __future__ import annotations

import pytest

from wekruit_matching.pipeline.health_gate import (
    PreSyncGateError,
    assert_pre_sync_ready,
)

pytestmark = pytest.mark.integration


def _dict_rows(conn):
    """Switch the WS-A pg_conn (default tuple rows) to dict_row.

    The production path opens connections with ``row_factory=dict_row``, and
    ``health_gate.compute_metrics`` / the gate's baseline reads access columns by
    name (``["count"]`` / ``["value"]``). The shared ``pg_conn`` fixture uses the
    default (tuple) factory, so we set dict_row on the connection here before
    exercising the gate. Subsequent ``conn.execute()`` cursors inherit it.
    """
    from psycopg.rows import dict_row

    conn.row_factory = dict_row
    return conn


_BASE_COLS = {
    "source_repo": "test",
    "company_name": "Acme",
    "role_title": "Engineer",
    "content_hash": "h",
    "status": "active",
}


def _insert_job(conn, job_id, **over):
    cols = {"job_id": job_id, **_BASE_COLS, **over}
    keys = ", ".join(cols)
    ph = ", ".join(f"%({k})s" for k in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({ph})", cols)


def _clean_matchable(job_id):
    """A fully matchable, invariant-clean active row."""
    return dict(
        job_id=job_id,
        job_description="x" * 300,
        required_skills=["python", "sql"],
        enriched_at="now()",
        embedding="[" + ",".join(["0.01"] * 1536) + "]",
        embedded_at="now()",
        embedding_model="text-embedding-3-small",
    )


def _isolate(conn):
    """Make the gate see ONLY this test's rows: park all pre-existing jobs in a
    status the gate ignores. Rolled back by the fixture, so prod data is intact.

    Also switches the connection to dict_row (every test calls this first), so
    health_gate's by-name column access works against the WS-A pg_conn fixture
    (which defaults to tuple rows).
    """
    _dict_rows(conn)
    conn.execute(
        "UPDATE jobs SET status = 'test_parked' WHERE status = 'active'"
    )


def test_stamp_without_verify_blocks_sync(pg_conn):
    """enriched_at + usable JD + ZERO skills -> PreSyncGateError (sync blocked)."""
    _isolate(pg_conn)
    # Seed a baseline so the run is past the first-run skip; otherwise only the
    # absolute invariants apply (which is exactly what we are testing anyway).
    pg_conn.execute(
        """
        INSERT INTO jobs_health_state (metric, value, updated_at)
        VALUES ('matchable', 0, now())
        ON CONFLICT (metric) DO UPDATE SET value = 0
        """
    )
    # Drop the 0010 constraint so we can plant a legacy dirty row.
    pg_conn.execute(
        "ALTER TABLE jobs DROP CONSTRAINT IF EXISTS "
        "ck_enriched_requires_skills_or_no_jd"
    )
    _insert_job(
        pg_conn,
        "presync-swv",
        job_description="x" * 300,
        required_skills=[],
        enriched_at="now()",
    )

    with pytest.raises(PreSyncGateError) as ei:
        assert_pre_sync_ready(pg_conn)
    assert "stamp_without_verify" in str(ei.value)


def test_embedded_without_vector_blocks_sync(pg_conn):
    """embedded_at set + NULL embedding -> PreSyncGateError."""
    _isolate(pg_conn)
    pg_conn.execute(
        "ALTER TABLE jobs DROP CONSTRAINT IF EXISTS ck_embedded_requires_vector"
    )
    _insert_job(
        pg_conn,
        "presync-env",
        job_description="x" * 300,
        required_skills=["python"],
        enriched_at="now()",
        embedded_at="now()",  # but no embedding
    )

    with pytest.raises(PreSyncGateError) as ei:
        assert_pre_sync_ready(pg_conn)
    assert "embedded_no_vector" in str(ei.value)


def test_clean_corpus_passes_and_writes_baseline(pg_conn):
    """A clean matchable row -> gate passes AND the matchable baseline is
    UPSERTed into jobs_health_state."""
    _isolate(pg_conn)
    # Start with no baseline -> first-run path (absolute invariants only).
    pg_conn.execute("DELETE FROM jobs_health_state WHERE metric = 'matchable'")
    # _clean_matchable already carries job_id; let _insert_job bind it from the
    # dict (passing it positionally too -> "multiple values for 'job_id'").
    _insert_job(pg_conn, **_clean_matchable("presync-clean"))

    # Should not raise.
    assert_pre_sync_ready(pg_conn) is None

    row = pg_conn.execute(
        "SELECT value FROM jobs_health_state WHERE metric = 'matchable'"
    ).fetchone()
    assert row is not None, "baseline row was not written"
    # Exactly our one matchable row is visible (others parked).
    assert int(row["value"]) == 1, row


def test_matchable_drop_below_floor_blocks(pg_conn):
    """With a persisted floor above the current matchable count, the gate blocks
    (corpus regression must not sync)."""
    _isolate(pg_conn)
    # Floor says we previously had 100 matchable; now we have 0 -> regression.
    pg_conn.execute(
        """
        INSERT INTO jobs_health_state (metric, value, updated_at)
        VALUES ('matchable', 100, now())
        ON CONFLICT (metric) DO UPDATE SET value = 100
        """
    )
    # No matchable rows inserted -> current matchable == 0 < 100.

    with pytest.raises(PreSyncGateError) as ei:
        assert_pre_sync_ready(pg_conn)
    assert "below persisted floor" in str(ei.value)


def test_first_run_skips_relative_floor(pg_conn):
    """No baseline row -> the relative floor is SKIPPED; a clean (even empty)
    corpus passes without blocking on a missing prior."""
    _isolate(pg_conn)
    pg_conn.execute("DELETE FROM jobs_health_state WHERE metric = 'matchable'")
    # No matchable rows; absolute invariants are all 0 -> must pass.
    assert_pre_sync_ready(pg_conn) is None
    # And it recorded the new baseline (0).
    row = pg_conn.execute(
        "SELECT value FROM jobs_health_state WHERE metric = 'matchable'"
    ).fetchone()
    assert row is not None and int(row["value"]) == 0
