"""Regression tests: first_seen_at must survive a job_id re-hash (recency signal).

Root cause (live data, 2026-05-29)
----------------------------------
``upsert_jobs`` set ``first_seen_at = now()`` on every INSERT and keyed updates
only on ``ON CONFLICT (job_id)``. ``job_id`` is a content hash of
(source_repo, norm company, norm role). When those inputs change -- the v1->v2
hash migration, or a source editing a company/title string -- the job re-hashes
to a NEW job_id, the conflict never fires, a fresh row is inserted with
``first_seen_at = now()`` and the original row is orphaned then stale-marked.
Net effect measured live: ALL 28,882 active jobs had first_seen_at within ~3
days (0 older than 1 day); 2,030 active rows had a first_seen_at newer than an
older sibling for the same identity. The recency signal the matcher depends on
was destroyed.

Fix: ``upsert_jobs`` carries forward the earliest ``first_seen_at`` recorded for
the job's stable identity ``(norm company, norm role, source_repo)``;
``check_first_seen_integrity`` is the runtime gate that auto-detects a
recurrence; ``backfill_first_seen`` is the idempotent one-time remediation.

These are integration tests against the live Postgres (same convention as
``test_scraper_upsert.py``); they skip if DATABASE_URL is unset / unreachable
and only ever touch ``test-%`` rows, which the autouse fixture deletes.
"""
from __future__ import annotations

import os
import time

import psycopg
import pytest
from psycopg.rows import dict_row

from wekruit_matching.models.job import Job
from wekruit_matching.scraper.id_utils import compute_content_hash
from wekruit_matching.scraper.upsert import (
    backfill_first_seen,
    check_first_seen_integrity,
    upsert_jobs,
)


def _conninfo() -> str:
    url = os.environ.get("DATABASE_URL", "")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _connect():
    conninfo = _conninfo()
    if not conninfo or conninfo == "postgresql://":
        pytest.skip("DATABASE_URL not set — skipping live first_seen tests")
    try:
        return psycopg.connect(conninfo, row_factory=dict_row)
    except Exception as e:  # pragma: no cover - env dependent
        pytest.skip(f"Cannot connect to DB: {e}")


def _make_job(
    job_id: str,
    company_name: str = "FirstSeenTestCo",
    role_title: str = "SWE Intern",
    source_repo: str = "Summer2026-Internships",
) -> Job:
    return Job(
        job_id=job_id,
        source_repo=source_repo,
        company_name=company_name,
        role_title=role_title,
        content_hash=compute_content_hash(company_name, role_title),
    )


@pytest.fixture(autouse=True)
def _cleanup():
    conn = _connect()
    conn.execute("DELETE FROM jobs WHERE job_id LIKE 'test-fsp-%'")
    conn.commit()
    yield
    conn.execute("DELETE FROM jobs WHERE job_id LIKE 'test-fsp-%'")
    conn.commit()
    conn.close()


def test_first_seen_preserved_across_job_id_rehash():
    """The core regression: re-inserting the same logical job under a NEW
    job_id (as a hash-scheme / title change would) keeps the ORIGINAL
    first_seen_at, even when the old row was stale-marked inactive."""
    old = _make_job("test-fsp-oldid", company_name="RehashCo", role_title="Data Analyst")
    with _connect() as conn:
        upsert_jobs([old], conn)
        # Backdate first_seen_at to simulate "seen weeks ago".
        conn.execute(
            "UPDATE jobs SET first_seen_at = now() - interval '20 days', "
            "last_seen_at = now() - interval '20 days', status = 'inactive' "
            "WHERE job_id = 'test-fsp-oldid'"
        )
        conn.commit()
        original = conn.execute(
            "SELECT first_seen_at FROM jobs WHERE job_id = 'test-fsp-oldid'"
        ).fetchone()["first_seen_at"]

    # Same company/title/source -> same identity, but a DIFFERENT job_id
    # (this is exactly what a re-hash produces).
    new = _make_job("test-fsp-newid", company_name="RehashCo", role_title="Data Analyst")
    with _connect() as conn:
        result = upsert_jobs([new], conn)
        assert result["inserted"] == 1  # genuinely a new job_id row
        new_fsa = conn.execute(
            "SELECT first_seen_at FROM jobs WHERE job_id = 'test-fsp-newid'"
        ).fetchone()["first_seen_at"]

    assert new_fsa == original, (
        "first_seen_at must be carried forward from the prior identity row; "
        f"got {new_fsa!r} instead of {original!r}"
    )


def test_first_seen_is_now_for_genuinely_new_job():
    """A brand-new identity (no prior row) gets first_seen_at ~= now()."""
    with _connect() as conn:
        before = conn.execute("SELECT now() AS n").fetchone()["n"]
        time.sleep(0.02)
        upsert_jobs(
            [_make_job("test-fsp-fresh", company_name="BrandNewCo", role_title="QA Eng")],
            conn,
        )
        fsa = conn.execute(
            "SELECT first_seen_at FROM jobs WHERE job_id = 'test-fsp-fresh'"
        ).fetchone()["first_seen_at"]
    assert fsa >= before


def test_check_first_seen_integrity_flags_reset_then_backfill_clears_it():
    """Runtime GATE detects a reset, and backfill repairs it (idempotently).

    We engineer a reset on isolated test rows and assert the gate's count rises
    by exactly the rows we created, backfill repairs exactly those rows, and a
    second backfill is a no-op. Asserting on the DELTA (not an absolute count)
    keeps the test robust against live-data offenders that exist concurrently.
    """
    with _connect() as conn:
        base = check_first_seen_integrity(conn)

        # Older sibling (inactive ghost) with the TRUE early first_seen_at...
        upsert_jobs(
            [_make_job("test-fsp-ghost", company_name="GateCo", role_title="ML Eng")],
            conn,
        )
        conn.execute(
            "UPDATE jobs SET first_seen_at = now() - interval '30 days', "
            "status = 'inactive' WHERE job_id = 'test-fsp-ghost'"
        )
        # ...and a newer active row for the same identity = the reset victim.
        upsert_jobs(
            [_make_job("test-fsp-victim", company_name="GateCo", role_title="ML Eng")],
            conn,
        )
        # Force the victim's first_seen_at to be recent (a fresh re-hash insert).
        conn.execute(
            "UPDATE jobs SET first_seen_at = now() - interval '1 day', "
            "status = 'active' WHERE job_id = 'test-fsp-victim'"
        )
        conn.commit()

        flagged = check_first_seen_integrity(conn)
        assert flagged == base + 1, (
            f"gate should flag exactly the 1 engineered victim (base={base}, "
            f"flagged={flagged})"
        )

        # Backfill repairs at least our victim (live offenders may also repair).
        repaired = backfill_first_seen(conn)
        conn.commit()
        assert repaired >= 1

        victim_fsa = conn.execute(
            "SELECT first_seen_at FROM jobs WHERE job_id = 'test-fsp-victim'"
        ).fetchone()["first_seen_at"]
        ghost_fsa = conn.execute(
            "SELECT first_seen_at FROM jobs WHERE job_id = 'test-fsp-ghost'"
        ).fetchone()["first_seen_at"]
        assert victim_fsa == ghost_fsa, "victim first_seen_at should now equal the ghost's"

        # Idempotency: re-running backfill changes nothing for our identity.
        before_ids = conn.execute(
            "SELECT job_id, first_seen_at FROM jobs WHERE job_id LIKE 'test-fsp-%' "
            "ORDER BY job_id"
        ).fetchall()
        backfill_first_seen(conn)
        conn.commit()
        after_ids = conn.execute(
            "SELECT job_id, first_seen_at FROM jobs WHERE job_id LIKE 'test-fsp-%' "
            "ORDER BY job_id"
        ).fetchall()
        assert before_ids == after_ids, "second backfill must be a no-op for our rows"
