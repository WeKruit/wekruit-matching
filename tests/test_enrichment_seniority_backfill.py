"""Regression tests: ``jobs.seniority_level`` must get populated in the
canonical vocab the live matcher consumes.

ROOT CAUSE (live data, 2026-05-29):
``jobs.seniority_level`` was NULL for 80.8% of active jobs (23,331 / 28,882).
``pipeline.job_sync`` SELECTs this column and writes it RAW (no normalization)
to the ``matching-jobs`` Firestore doc the live TypeScript matcher reads as
``seniorityLevel`` — so those NULLs blind the live seniority filter/scoring for
most of the corpus. The one title-based helper that existed
(``scraper.title_inference.infer_seniority``) emits a DIFFERENT vocabulary
(``entry_level``/``mid_level``/``junior``/...) that table-wide appears on only
6 rows, i.e. it effectively never lands in this column and is not what the
matcher consumes (``intern``/``entry``/``mid``/``senior``).

These tests guard:
  1. ``classify_seniority_from_title`` returns ONLY canonical vocab and reads
     the right cues (intern / entry / senior), defaulting cue-less titles to
     ``mid`` (NOT the wrong ``*_level`` vocab).
  2. ``backfill_seniority`` writes those labels to rows whose ``seniority_level``
     is NULL.
  3. The fill is NULL-only and idempotent — the UPDATE re-asserts
     ``seniority_level IS NULL`` so existing values are never overwritten.
  4. ``count_null_seniority_active`` reports the remaining NULL backlog so the
     "no zero-cost writer" failure mode is auto-caught on the next run.

They fail against pre-fix code because ``backfill_seniority`` /
``classify_seniority_from_title`` / ``count_null_seniority_active`` did not
exist (ImportError at collection time).
"""
from __future__ import annotations

import pytest

from wekruit_matching.enrichment.worker import (
    CANONICAL_SENIORITY,
    backfill_seniority,
    classify_seniority_from_title,
    count_null_seniority_active,
)


class _FakeCursor:
    def __init__(self, fetch_rows):
        self._fetch_rows = fetch_rows

    def fetchall(self):
        return self._fetch_rows

    def fetchone(self):
        return self._fetch_rows[0] if self._fetch_rows else None


class _FakeConn:
    """Minimal psycopg-conn stand-in.

    ``conn.execute(sql, params)`` records the call and returns a cursor. The
    first execute returns ``select_rows``; later executes (the per-row UPDATEs)
    return empty cursors.
    """

    def __init__(self, select_rows):
        self._select_rows = select_rows
        self._first = True
        self.executed: list[tuple[str, dict | None]] = []
        self.commits = 0

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        rows = self._select_rows if self._first else []
        self._first = False
        return _FakeCursor(rows)

    def commit(self):
        self.commits += 1


def _updates(conn: _FakeConn) -> list[dict]:
    return [p for (sql, p) in conn.executed if "UPDATE jobs" in sql and p is not None]


# --- classifier ------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Software Engineer Intern", "intern"),
        ("Summer 2026 Analyst", "intern"),
        ("New Grad Software Engineer", "entry"),
        ("Entry Level Data Analyst", "entry"),
        ("Junior Developer", "entry"),
        ("University Graduate Program", "entry"),
        ("Senior Backend Engineer", "senior"),
        ("Staff ML Engineer", "senior"),
        ("Principal Scientist", "senior"),
        ("Engineering Manager", "senior"),
        ("Director of Product", "senior"),
        # cue-less -> canonical default 'mid' (NOT 'mid_level')
        ("Software Engineer", "mid"),
        ("Account Executive", "mid"),
        ("", "mid"),
        (None, "mid"),
    ],
)
def test_classify_seniority_returns_canonical_vocab(title, expected):
    result = classify_seniority_from_title(title)
    assert result == expected
    # Hard guard against the wrong ``*_level`` vocabulary that never lands.
    assert result in CANONICAL_SENIORITY
    assert "_level" not in result


def test_canonical_vocab_is_exactly_the_four_matcher_buckets():
    assert CANONICAL_SENIORITY == {"intern", "entry", "mid", "senior"}


# --- backfill --------------------------------------------------------------


def test_backfill_writes_canonical_seniority_for_null_rows():
    rows = [
        ("j1", "Software Engineer Intern"),  # -> intern
        ("j2", "New Grad Software Engineer"),  # -> entry
        ("j3", "Senior Backend Engineer"),  # -> senior
        ("j4", "Software Engineer"),  # -> mid (default)
    ]
    conn = _FakeConn(select_rows=rows)
    updated = backfill_seniority(conn, limit=100)

    assert updated == 4
    assert conn.commits == 1
    written = {p["job_id"]: p["seniority_level"] for p in _updates(conn)}
    assert written == {"j1": "intern", "j2": "entry", "j3": "senior", "j4": "mid"}
    # Every written value is canonical.
    assert set(written.values()) <= CANONICAL_SENIORITY


def test_backfill_update_is_null_only_guarded():
    # Each UPDATE must carry ``seniority_level IS NULL`` so a concurrent/legacy
    # non-NULL value is never overwritten (idempotency + non-destruction).
    conn = _FakeConn(select_rows=[("j1", "Data Science Intern")])
    backfill_seniority(conn, limit=10)
    update_sqls = [sql for (sql, p) in conn.executed if "UPDATE jobs" in sql]
    assert update_sqls
    assert all("seniority_level IS NULL" in sql for sql in update_sqls)


def test_backfill_select_filters_active_and_null():
    conn = _FakeConn(select_rows=[])
    backfill_seniority(conn, limit=10)
    select_sql = conn.executed[0][0]
    assert "status = 'active'" in select_sql
    assert "seniority_level IS NULL" in select_sql


def test_backfill_no_rows_returns_zero_and_no_commit():
    conn = _FakeConn(select_rows=[])
    assert backfill_seniority(conn, limit=10) == 0
    assert _updates(conn) == []
    assert conn.commits == 0


# --- gate ------------------------------------------------------------------


def test_count_null_seniority_active_reads_scalar():
    conn = _FakeConn(select_rows=[(7,)])
    assert count_null_seniority_active(conn) == 7
    sql = conn.executed[0][0]
    assert "status = 'active'" in sql
    assert "seniority_level IS NULL" in sql
