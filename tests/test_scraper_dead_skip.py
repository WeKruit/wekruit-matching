"""Unit tests for P7-K Postgres dead-tombstone behaviour.

Covers:
  1. ``scraper.upsert._filter_dead_tombstoned`` — 30d skip / 90d retry /
     legacy-NULL skip / never-dead pass-through
  2. ``scraper.upsert.upsert_jobs`` integration: skipped rows don't reach
     UPSERT, retry rows do; ``skipped_dead_jobs`` log fires
  3. ``pipeline.dead_backfill.firestore_dead_backfill`` — Stage 0 mock
     of 5 dead Firestore docs → 5 Postgres UPDATEs; graceful skip on
     missing SDK / creds

Tests are pure unit (no live DB / Firestore). They use psycopg.Connection
mock fakes that capture executed SQL + params so we can assert exactly
which rows would have been touched.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from wekruit_matching.models.job import Job, JobStatus
from wekruit_matching.scraper.upsert import (
    _DEAD_RETRY_AGE_DAYS,
    _DEAD_RETRY_MAX_PER_RUN,
    _filter_dead_tombstoned,
    upsert_jobs,
)
from wekruit_matching.pipeline.dead_backfill import firestore_dead_backfill


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _job(job_id: str = "abc") -> Job:
    """Build a minimal Job suitable for upsert."""
    return Job(
        job_id=job_id,
        source_repo="test-source",
        company_name="Acme",
        role_title="Software Engineer",
        primary_url=f"https://example.com/jobs/{job_id}",
        location_raw="San Francisco, CA",
        date_posted_raw=None,
        status=JobStatus.ACTIVE,
        first_seen_at=datetime.now(UTC),
    )


class FakeConn:
    """Minimal psycopg.Connection stand-in.

    ``execute(sql, params)`` returns an object whose ``.fetchall()`` /
    ``.rowcount`` are pre-seeded by the test. ``cursor()`` returns a stub
    that records its ``executemany`` calls so tests can assert the upsert
    was (or wasn't) called.

    SQL is matched by substring — we look for distinctive phrases like
    "SELECT job_id, dead, dead_confirmed_at" to identify which call this is.
    """

    def __init__(self):
        self.commits = 0
        self.executed: list[tuple[str, dict | None]] = []
        self.executemany_batches: list[tuple[str, list[dict]]] = []
        # Pre-seeded responses keyed by substring match.
        self._select_dead_rows: list[dict] = []
        self._select_existing_hash_rows: list[dict] = []

    def commit(self):
        self.commits += 1

    def cursor(self):
        outer = self

        class _Cur:
            def executemany(self, sql, batch):
                outer.executemany_batches.append((sql, list(batch)))

        return _Cur()

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

        # Return a result object whose .fetchall / .rowcount the call site
        # may use. The dispatch is by SQL substring.
        result = MagicMock()
        if "SELECT job_id, dead, dead_confirmed_at" in sql:
            result.fetchall.return_value = list(self._select_dead_rows)
        elif "SELECT job_id, content_hash" in sql:
            result.fetchall.return_value = list(self._select_existing_hash_rows)
        elif "UPDATE jobs" in sql and "dead_confirmed_at" in sql:
            # Stage-0 backfill UPDATE or 90d-retry reset
            result.rowcount = len(params.get("ids", [])) if params else 0
        elif "UPDATE jobs a" in sql and "ats_apply_url = b.ats_apply_url" in sql:
            # Carry-forward block
            result.rowcount = 0
        else:
            result.rowcount = 0
            result.fetchall.return_value = []
        return result


# ---------------------------------------------------------------------------
# 1. _filter_dead_tombstoned
# ---------------------------------------------------------------------------


def test_filter_skips_dead_in_30d_window():
    """dead=true with confirmed_at 10 days ago → skip, not retried."""
    conn = FakeConn()
    conn._select_dead_rows = [
        {
            "job_id": "skip-me",
            "dead": True,
            "dead_confirmed_at": datetime.now(UTC) - timedelta(days=10),
        }
    ]
    jobs = [_job("skip-me"), _job("keep-me")]
    filtered, skipped, retried = _filter_dead_tombstoned(jobs, conn)

    assert skipped == 1
    assert retried == 0
    assert {j.job_id for j in filtered} == {"keep-me"}
    # No retry UPDATE should have fired
    update_dead_resets = [
        e for e in conn.executed
        if "UPDATE jobs" in e[0] and "dead = FALSE" in e[0]
    ]
    assert update_dead_resets == []


def test_filter_skips_dead_in_30_to_90d_window():
    """dead=true with confirmed_at 60 days ago → skip (retry only at 90d+)."""
    conn = FakeConn()
    conn._select_dead_rows = [
        {
            "job_id": "skip-mid",
            "dead": True,
            "dead_confirmed_at": datetime.now(UTC) - timedelta(days=60),
        }
    ]
    jobs = [_job("skip-mid"), _job("normal")]
    filtered, skipped, retried = _filter_dead_tombstoned(jobs, conn)

    assert skipped == 1
    assert retried == 0
    assert {j.job_id for j in filtered} == {"normal"}


def test_filter_retries_dead_past_90d():
    """dead=true with confirmed_at 120 days ago → retry path, kept in input."""
    conn = FakeConn()
    conn._select_dead_rows = [
        {
            "job_id": "retry-me",
            "dead": True,
            "dead_confirmed_at": datetime.now(UTC) - timedelta(days=120),
        }
    ]
    jobs = [_job("retry-me"), _job("normal")]
    filtered, skipped, retried = _filter_dead_tombstoned(jobs, conn)

    assert skipped == 0
    assert retried == 1
    assert {j.job_id for j in filtered} == {"retry-me", "normal"}

    # Retry UPDATE must have fired with the right id
    resets = [
        (sql, params) for sql, params in conn.executed
        if "UPDATE jobs" in sql and "dead = FALSE" in sql
    ]
    assert len(resets) == 1
    assert resets[0][1]["ids"] == ["retry-me"]


def test_filter_skips_dead_with_null_confirmed_at():
    """Legacy backfill with dead=true but dead_confirmed_at=NULL → skip
    (we treat unknown age as recent for safety)."""
    conn = FakeConn()
    conn._select_dead_rows = [
        {
            "job_id": "legacy",
            "dead": True,
            "dead_confirmed_at": None,
        }
    ]
    jobs = [_job("legacy")]
    filtered, skipped, retried = _filter_dead_tombstoned(jobs, conn)

    assert skipped == 1
    assert retried == 0
    assert filtered == []


def test_filter_passes_through_non_dead_jobs():
    """No row in jobs.dead==true SELECT → all jobs pass through unchanged."""
    conn = FakeConn()
    conn._select_dead_rows = []  # empty
    jobs = [_job("a"), _job("b"), _job("c")]
    filtered, skipped, retried = _filter_dead_tombstoned(jobs, conn)

    assert skipped == 0
    assert retried == 0
    assert {j.job_id for j in filtered} == {"a", "b", "c"}


def test_filter_caps_retries_at_max_per_run():
    """If 150 rows are eligible for 90d retry, only 100 retry; rest skip."""
    conn = FakeConn()
    old = datetime.now(UTC) - timedelta(days=200)
    conn._select_dead_rows = [
        {"job_id": f"old-{i}", "dead": True, "dead_confirmed_at": old}
        for i in range(150)
    ]
    jobs = [_job(f"old-{i}") for i in range(150)]
    filtered, skipped, retried = _filter_dead_tombstoned(jobs, conn)

    assert retried == _DEAD_RETRY_MAX_PER_RUN  # 100
    assert skipped == 150 - _DEAD_RETRY_MAX_PER_RUN  # 50
    assert len(filtered) == _DEAD_RETRY_MAX_PER_RUN


def test_filter_handles_empty_input():
    conn = FakeConn()
    filtered, skipped, retried = _filter_dead_tombstoned([], conn)
    assert filtered == []
    assert skipped == 0
    assert retried == 0
    # Should not have queried jobs at all
    assert conn.executed == []


# ---------------------------------------------------------------------------
# 2. upsert_jobs integration
# ---------------------------------------------------------------------------


def test_upsert_skipped_dead_does_not_reach_executemany():
    """Verify skipped jobs are removed from the input that reaches the
    INSERT...ON CONFLICT executemany call."""
    conn = FakeConn()
    conn._select_dead_rows = [
        {
            "job_id": "dead-1",
            "dead": True,
            "dead_confirmed_at": datetime.now(UTC) - timedelta(days=5),
        }
    ]
    conn._select_existing_hash_rows = []  # neither row exists yet

    jobs = [_job("dead-1"), _job("alive-1")]
    stats = upsert_jobs(jobs, conn)

    assert stats["skipped_dead"] == 1
    assert stats["dead_retried"] == 0

    # The executemany call (UPSERT) must have run with only "alive-1".
    assert len(conn.executemany_batches) == 1
    sql, batch = conn.executemany_batches[0]
    assert "INSERT INTO jobs" in sql
    assert {row["job_id"] for row in batch} == {"alive-1"}


def test_upsert_returns_zero_counts_when_all_dead():
    """If every input is tombstoned, upsert short-circuits without touching
    the INSERT path."""
    conn = FakeConn()
    conn._select_dead_rows = [
        {
            "job_id": "d1",
            "dead": True,
            "dead_confirmed_at": datetime.now(UTC) - timedelta(days=2),
        },
        {
            "job_id": "d2",
            "dead": True,
            "dead_confirmed_at": datetime.now(UTC) - timedelta(days=2),
        },
    ]

    jobs = [_job("d1"), _job("d2")]
    stats = upsert_jobs(jobs, conn)

    assert stats == {
        "inserted": 0, "updated": 0, "unchanged": 0,
        "skipped_dead": 2, "dead_retried": 0,
    }
    assert conn.executemany_batches == []  # never reached UPSERT


def test_upsert_retry_path_lets_old_dead_re_upsert():
    """120d-old dead URL is reset, then UPSERTed normally so it re-activates."""
    conn = FakeConn()
    old = datetime.now(UTC) - timedelta(days=120)
    conn._select_dead_rows = [
        {"job_id": "retry-me", "dead": True, "dead_confirmed_at": old}
    ]
    conn._select_existing_hash_rows = [
        {"job_id": "retry-me", "content_hash": "old-hash"}
    ]

    jobs = [_job("retry-me")]
    jobs[0].content_hash = "new-hash"  # so it counts as updated

    stats = upsert_jobs(jobs, conn)

    assert stats["skipped_dead"] == 0
    assert stats["dead_retried"] == 1
    # The retry UPDATE (dead=FALSE reset) must have fired
    resets = [
        e for e in conn.executed
        if "UPDATE jobs" in e[0] and "dead = FALSE" in e[0]
    ]
    assert len(resets) == 1
    # The UPSERT must have processed the row
    assert len(conn.executemany_batches) == 1


def test_upsert_empty_input_returns_zero_dead_counts():
    conn = FakeConn()
    stats = upsert_jobs([], conn)
    assert stats == {
        "inserted": 0, "updated": 0, "unchanged": 0,
        "skipped_dead": 0, "dead_retried": 0,
    }


# ---------------------------------------------------------------------------
# 3. firestore_dead_backfill (Stage 0)
# ---------------------------------------------------------------------------


def test_stage0_backfills_5_dead_docs():
    """Mock Firestore returns 5 dead, Stage 0 issues UPDATEs covering 5 rows."""
    conn = FakeConn()
    fixed_ts = datetime(2026, 5, 1, tzinfo=UTC)
    fake_docs = [(f"job-{i}", fixed_ts) for i in range(5)]

    def factory():
        return iter(fake_docs)

    stats = firestore_dead_backfill(conn, iter_factory=factory, batch_size=10)

    assert stats["total_seen"] == 5
    assert stats["synced"] == 5
    assert stats["skipped"] == ""
    # One UPDATE call (under batch_size threshold)
    update_calls = [
        e for e in conn.executed
        if "UPDATE jobs" in e[0] and "dead = TRUE" in e[0]
    ]
    assert len(update_calls) == 1
    sql, params = update_calls[0]
    assert sorted(params["ids"]) == [f"job-{i}" for i in range(5)]


def test_stage0_batches_when_set_exceeds_batch_size():
    """If batch_size=2 and Firestore returns 5 dead docs, expect 3 UPDATE calls."""
    conn = FakeConn()
    fixed_ts = datetime(2026, 5, 1, tzinfo=UTC)
    fake_docs = [(f"job-{i}", fixed_ts) for i in range(5)]

    def factory():
        return iter(fake_docs)

    stats = firestore_dead_backfill(conn, iter_factory=factory, batch_size=2)

    assert stats["total_seen"] == 5
    assert stats["synced"] == 5
    update_calls = [
        e for e in conn.executed
        if "UPDATE jobs" in e[0] and "dead = TRUE" in e[0]
    ]
    assert len(update_calls) == 3  # 2 + 2 + 1


def test_stage0_graceful_skip_when_sdk_missing():
    """If the iterator factory raises ImportError (SDK not installed),
    Stage 0 returns zero-skip stats without touching Postgres."""
    conn = FakeConn()

    def factory():
        raise ImportError("google-cloud-firestore not installed")

    stats = firestore_dead_backfill(conn, iter_factory=factory)

    assert stats == {"synced": 0, "total_seen": 0, "skipped": "no_sdk"}
    # No UPDATE / SELECT against jobs table
    assert conn.executed == []


def test_stage0_graceful_skip_when_creds_missing():
    """Generic Exception (e.g. DefaultCredentialsError) → skip with no_creds."""
    conn = FakeConn()

    def factory():
        raise RuntimeError("Could not automatically determine credentials")

    stats = firestore_dead_backfill(conn, iter_factory=factory)

    assert stats == {"synced": 0, "total_seen": 0, "skipped": "no_creds"}
    assert conn.executed == []


def test_stage0_partial_failure_flushes_what_it_has():
    """If the iterator raises mid-stream, the rows already accumulated in
    ``pending`` should still be flushed via the best-effort UPDATE."""
    conn = FakeConn()
    fixed_ts = datetime(2026, 5, 1, tzinfo=UTC)

    def factory():
        # Yield 3 then raise
        yield ("job-0", fixed_ts)
        yield ("job-1", fixed_ts)
        yield ("job-2", fixed_ts)
        raise ConnectionError("Firestore stream broke")

    stats = firestore_dead_backfill(conn, iter_factory=factory, batch_size=10)

    # All 3 seen; partial flush attempted.
    assert stats["total_seen"] == 3
    assert stats["synced"] == 3
