"""Integration tests for upsert_jobs() and mark_stale_jobs() (SCRP-08).

These tests require a live Postgres instance.
Run with: uv run pytest tests/test_scraper_upsert.py -v

Tests skip automatically if DATABASE_URL is not set or DB is unreachable.
"""
import os
import pytest
import psycopg
from psycopg.rows import dict_row

from wekruit_matching.models.job import Job, JobStatus
from wekruit_matching.scraper.id_utils import compute_content_hash, generate_job_id
from wekruit_matching.scraper.upsert import mark_stale_jobs, upsert_jobs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_conninfo() -> str:
    """Convert SQLAlchemy URL to libpq format."""
    url = os.environ.get("DATABASE_URL", "")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _connect():
    """Return a psycopg3 connection or skip if DB is unavailable."""
    conninfo = get_conninfo()
    if not conninfo or conninfo == "postgresql://":
        pytest.skip("DATABASE_URL not set — skipping DB upsert tests")
    try:
        return psycopg.connect(conninfo, row_factory=dict_row)
    except Exception as e:
        pytest.skip(f"Cannot connect to DB: {e}")


def make_job(
    job_id: str = "test-job-001",
    company_name: str = "TestCo",
    role_title: str = "SWE Intern",
    content_hash: str | None = None,
    source_repo: str = "Summer2026-Internships",
) -> Job:
    """Build a minimal Job object for testing."""
    return Job(
        job_id=job_id,
        source_repo=source_repo,
        company_name=company_name,
        role_title=role_title,
        content_hash=content_hash or compute_content_hash(company_name, role_title),
    )


@pytest.fixture(autouse=True)
def cleanup_test_jobs():
    """Remove all test-* jobs before and after each test to ensure clean state."""
    conn = _connect()
    conn.execute("DELETE FROM jobs WHERE job_id LIKE 'test-%'")
    conn.commit()
    yield
    conn.execute("DELETE FROM jobs WHERE job_id LIKE 'test-%'")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_upsert_inserts_new_job():
    """Test 1: upsert_jobs with a new job_id inserts a row; COUNT returns 1."""
    job = make_job(job_id="test-job-001")
    with _connect() as conn:
        result = upsert_jobs([job], conn)

    assert result["inserted"] == 1
    assert result["updated"] == 0
    assert result["unchanged"] == 0

    # Verify via direct DB query
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM jobs WHERE job_id = 'test-job-001'"
        ).fetchone()
    assert row["cnt"] == 1


def test_upsert_no_duplicate_on_second_call():
    """Test 2: calling upsert_jobs twice on same job_id does NOT create a duplicate."""
    job = make_job(job_id="test-job-001")
    with _connect() as conn:
        upsert_jobs([job], conn)
    with _connect() as conn:
        upsert_jobs([job], conn)

    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM jobs WHERE job_id = 'test-job-001'"
        ).fetchone()
    assert row["cnt"] == 1, "Duplicate row inserted on second upsert call"


def test_upsert_updates_changed_content_hash():
    """Test 3: upsert with same job_id but different content_hash updates hash and last_seen_at,
    but does NOT change first_seen_at."""
    job_v1 = make_job(
        job_id="test-job-001",
        company_name="TestCo",
        role_title="SWE Intern",
        content_hash=compute_content_hash("TestCo", "SWE Intern"),
    )
    with _connect() as conn:
        upsert_jobs([job_v1], conn)

    # Read first_seen_at after insert
    with _connect() as conn:
        row_v1 = conn.execute(
            "SELECT first_seen_at, last_seen_at, content_hash FROM jobs WHERE job_id = 'test-job-001'"
        ).fetchone()
    first_seen_original = row_v1["first_seen_at"]
    old_hash = row_v1["content_hash"]

    # Now upsert same job_id with a different content_hash (role title changed)
    job_v2 = make_job(
        job_id="test-job-001",
        company_name="TestCo",
        role_title="Senior SWE",  # Different role title -> different hash
        content_hash=compute_content_hash("TestCo", "Senior SWE"),
    )
    import time
    time.sleep(0.05)  # Ensure a timestamp difference is possible
    with _connect() as conn:
        result = upsert_jobs([job_v2], conn)

    assert result["updated"] == 1
    assert result["inserted"] == 0

    with _connect() as conn:
        row_v2 = conn.execute(
            "SELECT first_seen_at, last_seen_at, content_hash FROM jobs WHERE job_id = 'test-job-001'"
        ).fetchone()

    # content_hash must change
    assert row_v2["content_hash"] != old_hash, "content_hash should have been updated"
    assert row_v2["content_hash"] == compute_content_hash("TestCo", "Senior SWE")

    # first_seen_at must NOT change
    assert row_v2["first_seen_at"] == first_seen_original, (
        "first_seen_at should not change on update"
    )


def test_upsert_noop_on_unchanged_hash():
    """Test 4: upsert with same job_id and identical content_hash is a no-op — no update."""
    job = make_job(job_id="test-job-001")
    with _connect() as conn:
        upsert_jobs([job], conn)

    with _connect() as conn:
        row_before = conn.execute(
            "SELECT last_seen_at, content_hash FROM jobs WHERE job_id = 'test-job-001'"
        ).fetchone()

    # Upsert again with exact same hash
    with _connect() as conn:
        result = upsert_jobs([job], conn)

    assert result["unchanged"] == 1, "Expected unchanged=1 when hash is identical"
    assert result["updated"] == 0

    with _connect() as conn:
        row_after = conn.execute(
            "SELECT last_seen_at, content_hash FROM jobs WHERE job_id = 'test-job-001'"
        ).fetchone()

    # content_hash must not change
    assert row_after["content_hash"] == row_before["content_hash"]


def test_mark_stale_jobs_marks_missing_ids_inactive():
    """Test 5: mark_stale_jobs sets status='inactive' for rows NOT in seen_ids,
    while rows IN seen_ids remain status='active'."""
    job_a = make_job(job_id="test-job-a", company_name="CompanyA")
    job_b = make_job(job_id="test-job-b", company_name="CompanyB")
    job_c = make_job(job_id="test-job-c", company_name="CompanyC")

    with _connect() as conn:
        upsert_jobs([job_a, job_b, job_c], conn)

    # Simulate: only job_a is in the latest scrape; job_b and job_c have disappeared
    seen_ids = {"test-job-a"}
    with _connect() as conn:
        stale_count = mark_stale_jobs(seen_ids, "Summer2026-Internships", conn)

    assert stale_count == 2, f"Expected 2 stale rows, got {stale_count}"

    with _connect() as conn:
        rows = conn.execute(
            "SELECT job_id, status FROM jobs WHERE job_id LIKE 'test-job-%' ORDER BY job_id"
        ).fetchall()

    statuses = {r["job_id"]: r["status"] for r in rows}
    assert statuses["test-job-a"] == "active", "test-job-a should remain active"
    assert statuses["test-job-b"] == "inactive", "test-job-b should be marked inactive"
    assert statuses["test-job-c"] == "inactive", "test-job-c should be marked inactive"


def test_mark_stale_jobs_does_not_affect_other_repos():
    """Test 6: mark_stale_jobs scoped to source_repo — does NOT affect other repos."""
    # Insert one internships job and one new-grad job
    internship_job = make_job(
        job_id="test-job-intern",
        company_name="InternCo",
        source_repo="Summer2026-Internships",
    )
    newgrad_job = make_job(
        job_id="test-job-newgrad",
        company_name="NewGradCo",
        source_repo="New-Grad-Positions",
    )

    with _connect() as conn:
        upsert_jobs([internship_job, newgrad_job], conn)

    # Mark stale for Summer2026-Internships only (seen_ids is empty — all should go stale)
    with _connect() as conn:
        stale_count = mark_stale_jobs(set(), "Summer2026-Internships", conn)

    assert stale_count == 1, f"Expected 1 stale row (internship only), got {stale_count}"

    with _connect() as conn:
        intern_row = conn.execute(
            "SELECT status FROM jobs WHERE job_id = 'test-job-intern'"
        ).fetchone()
        newgrad_row = conn.execute(
            "SELECT status FROM jobs WHERE job_id = 'test-job-newgrad'"
        ).fetchone()

    assert intern_row["status"] == "inactive", "Internship job should be inactive"
    assert newgrad_row["status"] == "active", "New-grad job must NOT be affected by internships stale run"
