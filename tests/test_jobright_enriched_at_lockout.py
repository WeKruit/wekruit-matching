"""Regression test for the 2026-06-01 JobRight no-skills lockout.

Root cause: ``enrich_from_jobright.enrich_all_jobs`` stamped ``enriched_at = NOW()``
unconditionally, even when the fetched detail produced ZERO skills. A JD-bearing
row with empty ``required_skills`` then (a) hid behind the staleness gate and
(b) failed the embed gate ``cardinality(required_skills) > 0`` — locking it out
of the matching pool. The daily pipeline (Stage 2a) re-created ~1,900 such rows
on 2026-06-01.

Fix: only stamp ``enriched_at`` when skills were extracted; leave it NULL on an
empty-skills miss so the Stage 2c LLM gap-fill pass recovers the row. An in-run
``attempted_ids`` set prevents the now-NULL rows from being re-SELECTed forever.

These tests use a fake connection that records every UPDATE statement, so they
assert the emitted SQL directly with no live DB.
"""
from __future__ import annotations

import wekruit_matching.scraper.enrich_from_jobright as ej


class _FakeCursor:
    """Records executed statements; serves canned SELECT results."""

    def __init__(self, select_batches):
        # select_batches: list of row-lists returned by successive SELECT calls
        self._select_batches = list(select_batches)
        self.updates: list[tuple[str, dict]] = []
        self.rowcount = 0

    def fetchone(self):
        # Only the initial COUNT(*) query hits fetchone in enrich_all_jobs.
        return {"c": sum(len(b) for b in self._select_batches)}

    def fetchall(self):
        if self._select_batches:
            return self._select_batches.pop(0)
        return []


class _FakeConn:
    def __init__(self, select_batches):
        self._cursor = _FakeCursor(select_batches)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.upper().startswith("SELECT COUNT"):
            return self._cursor
        if s.upper().startswith("SELECT"):
            return self._cursor
        if s.upper().startswith("UPDATE"):
            self._cursor.updates.append((s, params or {}))
            return self._cursor
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    @property
    def updates(self):
        return self._cursor.updates


def _patch_fetch(monkeypatch, *, skills, jd_text="A sufficiently long job description. " * 10):
    """Make _fetch_job_detail return a detail dict with the given skills + JD."""
    def _fake(url):
        return {
            "skills": skills,
            "jd_text": jd_text,
            "responsibilities": [],
            "qualifications": [],
            "industry_list": [],
            "seniority": "",
            "salary": "",
            "benefits": [],
            "sponsorship": None,
            "work_model": None,
        }

    monkeypatch.setattr(ej, "_fetch_job_detail", _fake)
    # No real sleeping between batches.
    monkeypatch.setattr(ej.time, "sleep", lambda *_a, **_k: None)


def test_empty_skills_does_not_stamp_enriched_at(monkeypatch) -> None:
    """A JobRight job that yields ZERO skills must NOT get enriched_at = NOW()."""
    _patch_fetch(monkeypatch, skills=[])
    # One batch of one job, then empty (loop terminates via attempted_ids guard).
    conn = _FakeConn([[{"job_id": "j1", "primary_url": "https://jobright.ai/x", "role_title": "Eng"}], []])

    ej.enrich_all_jobs(conn, max_workers=1, batch_size=50)

    assert len(conn.updates) == 1, "expected exactly one UPDATE"
    sql, _params = conn.updates[0]
    assert "enriched_at = NOW()" not in sql, (
        "empty-skills row must NOT stamp enriched_at (the lockout bug)"
    )
    assert "required_skills" in sql


def test_nonempty_skills_stamps_enriched_at(monkeypatch) -> None:
    """A JobRight job WITH skills AND a usable JD must stamp enriched_at = NOW()."""
    _patch_fetch(monkeypatch, skills=["python", "sql"])
    conn = _FakeConn([[{"job_id": "j2", "primary_url": "https://jobright.ai/y", "role_title": "Eng"}], []])

    ej.enrich_all_jobs(conn, max_workers=1, batch_size=50)

    assert len(conn.updates) == 1
    sql, _params = conn.updates[0]
    assert "enriched_at = NOW()" in sql, "skills + usable-JD row must stamp enriched_at"
    assert "job_description = %(jd_text)s" in sql, "usable JD must be persisted"


def test_skills_with_short_jd_does_not_stamp_or_persist_thin_jd(monkeypatch) -> None:
    """rank-4: skills present BUT a short (<200) JD must NOT stamp enriched_at
    (would fail the embed length gate -> lockout from the other side) and must
    NOT persist the thin JD (so Stage 2b re-fetches the full page)."""
    _patch_fetch(monkeypatch, skills=["python", "sql"], jd_text="too short")
    conn = _FakeConn([[{"job_id": "j3", "primary_url": "https://jobright.ai/z", "role_title": "Eng"}], []])

    ej.enrich_all_jobs(conn, max_workers=1, batch_size=50)

    assert len(conn.updates) == 1
    sql, _params = conn.updates[0]
    assert "enriched_at = NOW()" not in sql, (
        "skills + SHORT JD must NOT stamp enriched_at (embed length gate would lock it out)"
    )
    assert "job_description = %(jd_text)s" not in sql, (
        "a thin JD must NOT be persisted (keeps the row eligible for Stage 2b re-fetch)"
    )
    assert "required_skills" in sql


def test_empty_skills_run_terminates(monkeypatch) -> None:
    """The attempted_ids guard prevents an infinite loop when rows stay NULL.

    Without the guard, leaving enriched_at NULL on an empty-skills row would make
    the WHERE enriched_at IS NULL SELECT return the same row forever. The fake
    SELECT always returns the same row unless excluded — so if the guard works,
    the run completes; if not, the test hangs / overruns the update budget.
    """
    _patch_fetch(monkeypatch, skills=[])

    row = {"job_id": "loop1", "primary_url": "https://jobright.ai/z", "role_title": "Eng"}

    class _LoopCursor(_FakeCursor):
        def fetchall(self):
            # Always return the row UNLESS it's been excluded via attempted set.
            # enrich_all_jobs passes attempted as a param to the SELECT; emulate
            # exclusion by checking the last SELECT params recorded on the conn.
            if getattr(self, "_excluded", False):
                return []
            return [row]

    class _LoopConn(_FakeConn):
        def __init__(self):
            self._cursor = _LoopCursor([])

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.upper().startswith("SELECT COUNT"):
                return self._cursor
            if s.upper().startswith("SELECT"):
                attempted = (params or {}).get("attempted") or []
                self._cursor._excluded = "loop1" in attempted
                return self._cursor
            if s.upper().startswith("UPDATE"):
                self._cursor.updates.append((s, params or {}))
                return self._cursor
            return self._cursor

    conn = _LoopConn()
    ej.enrich_all_jobs(conn, max_workers=1, batch_size=50)

    # Exactly one UPDATE — the row was attempted once, then excluded → loop ends.
    assert len(conn.updates) == 1, f"expected 1 update, got {len(conn.updates)} (guard failed → loop)"
