"""Regression tests for the mark_stale_jobs circuit-breaker (reliability audit
2026-06-01, ranks 9-13: partial-scrape mass-deactivation).

A partial/failed scrape returns a truncated seen-set; without a guard,
mark_stale_jobs flips every active row NOT in that set to inactive, silently
mass-deactivating live jobs. The circuit-breaker refuses to deactivate when a
single run would inactivate more than _STALE_MAX_DEACTIVATION_FRACTION of a
repo's active rows (above a min-active floor), returning STALE_GUARD_TRIPPED.

Mock-conn (no live DB): a fake connection answers the active-count + would-
deactivate COUNT queries and records any UPDATE so we can assert nothing was
deactivated when the guard trips.
"""
from __future__ import annotations

import wekruit_matching.scraper.upsert as up
from wekruit_matching.scraper.upsert import (
    STALE_GUARD_TRIPPED,
    mark_stale_jobs,
)


class _FakeResult:
    def __init__(self, row=None, rowcount=0):
        self._row = row
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


class _FakeConn:
    """Answers the guard's COUNT queries; records UPDATEs that would deactivate.

    active_n: rows currently active for the repo.
    would_n: rows this run would deactivate (active AND NOT in seen).
    """

    def __init__(self, *, active_n: int, would_n: int):
        self.active_n = active_n
        self.would_n = would_n
        self.updates: list[str] = []
        self.commits = 0

    def execute(self, sql: str, params=None):
        s = " ".join(sql.split()).lower()
        if s.startswith("update"):
            self.updates.append(s)
            return _FakeResult(rowcount=self.would_n)
        if "count(*)" in s and "not (job_id = any" in s:
            return _FakeResult(row={"n": self.would_n})
        if "count(*)" in s:  # active count
            return _FakeResult(row={"n": self.active_n})
        if "select job_id" in s:  # large-set path active id collection
            return _FakeResult()
        return _FakeResult()

    def commit(self):
        self.commits += 1


def test_guard_trips_on_partial_scrape_deactivating_majority():
    """100 active, scrape saw only 10 -> would deactivate 90 (90% > 50%) ->
    guard trips, NOTHING deactivated, returns sentinel."""
    conn = _FakeConn(active_n=100, would_n=90)
    result = mark_stale_jobs({f"id-{i}" for i in range(10)}, "repo-x", conn)
    assert result == STALE_GUARD_TRIPPED
    assert conn.updates == [], "guard must NOT issue any deactivation UPDATE"


def test_guard_allows_normal_churn():
    """100 active, scrape saw 95 -> would deactivate 5 (5% < 50%) -> proceeds."""
    conn = _FakeConn(active_n=100, would_n=5)
    result = mark_stale_jobs({f"id-{i}" for i in range(95)}, "repo-x", conn)
    assert result != STALE_GUARD_TRIPPED
    assert any(u.startswith("update") for u in conn.updates), "normal churn must deactivate"


def test_guard_skips_below_min_active_floor():
    """Tiny repo (3 active) below the min-active floor -> guard does NOT engage
    even though seen-set is empty (legit small-board full churn)."""
    conn = _FakeConn(active_n=3, would_n=3)
    # empty seen-set hits the all-disappeared branch; with active<floor the guard
    # is skipped and the full-clear UPDATE runs.
    result = mark_stale_jobs(set(), "tiny-repo", conn)
    assert result != STALE_GUARD_TRIPPED


def test_force_overrides_guard():
    """force=True bypasses the circuit-breaker for a legitimate bulk clear."""
    conn = _FakeConn(active_n=100, would_n=90)
    result = mark_stale_jobs({"id-1"}, "repo-x", conn, force=True)
    assert result != STALE_GUARD_TRIPPED
    assert any(u.startswith("update") for u in conn.updates)


def test_guard_threshold_boundary_just_over_trips():
    """Exactly at the 50% boundary passes; just over trips. 100 active:
    50 deactivated = 50% (not > 50%) proceeds; 51 = 51% trips."""
    ok = _FakeConn(active_n=100, would_n=50)
    assert mark_stale_jobs({f"id-{i}" for i in range(50)}, "repo", ok) != STALE_GUARD_TRIPPED

    trip = _FakeConn(active_n=100, would_n=51)
    assert mark_stale_jobs({f"id-{i}" for i in range(49)}, "repo", trip) == STALE_GUARD_TRIPPED
    assert trip.updates == []
