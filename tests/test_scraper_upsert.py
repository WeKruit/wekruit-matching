"""Integration tests for upsert_jobs() and mark_stale_jobs() (SCRP-08).

These tests require a live Postgres instance.
Run with: uv run pytest tests/test_scraper_upsert.py -v

Tests skip automatically if DATABASE_URL is not set or DB is unreachable.
"""
import os

import psycopg
import pytest
from psycopg.rows import dict_row

from wekruit_matching.models.job import Job
from wekruit_matching.scraper.id_utils import compute_content_hash
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
            """
            SELECT first_seen_at, last_seen_at, content_hash
            FROM jobs
            WHERE job_id = 'test-job-001'
            """
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
            """
            SELECT first_seen_at, last_seen_at, content_hash
            FROM jobs
            WHERE job_id = 'test-job-001'
            """
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


def test_upsert_changed_content_hash_clears_embedding_state():
    """Changed content_hash must force a fresh embed by clearing old embedding state."""
    job_v1 = make_job(
        job_id="test-job-embed-reset",
        company_name="TestCo",
        role_title="SWE Intern",
        content_hash=compute_content_hash("TestCo", "SWE Intern"),
    )
    job_v2 = make_job(
        job_id="test-job-embed-reset",
        company_name="TestCo",
        role_title="Senior SWE",
        content_hash=compute_content_hash("TestCo", "Senior SWE"),
    )

    with _connect() as conn:
        upsert_jobs([job_v1], conn)
        conn.execute(
            """
            UPDATE jobs
            SET embedding = array_fill(0.1::float4, ARRAY[1536])::vector,
                embedding_model = 'text-embedding-3-small',
                enriched_at = NOW(),
                embedded_at = NOW()
            WHERE job_id = 'test-job-embed-reset'
            """
        )
        conn.commit()

    with _connect() as conn:
        upsert_jobs([job_v2], conn)

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT enriched_at,
                   embedded_at,
                   embedding_model,
                   embedding IS NULL AS embedding_is_null
            FROM jobs
            WHERE job_id = 'test-job-embed-reset'
            """
        ).fetchone()

    assert row["enriched_at"] is None
    assert row["embedded_at"] is None
    assert row["embedding_model"] is None
    assert row["embedding_is_null"] is True


def test_upsert_changed_hash_clears_permanent_404_and_terminal_source():
    """rank-16: a re-listed posting (content_hash CHANGED) must clear the
    permanent_404 tombstone + the terminal jd_fetch_source sentinel so the JD
    queue re-admits it (otherwise it thrashes active<->inactive forever)."""
    job_v1 = make_job(
        job_id="test-job-relist",
        company_name="RelistCo",
        role_title="SWE Intern",
        content_hash=compute_content_hash("RelistCo", "SWE Intern"),
    )
    with _connect() as conn:
        upsert_jobs([job_v1], conn)
        # Tombstone it as closed-at-source (the unrecoverable state pre-fix).
        conn.execute(
            """
            UPDATE jobs
            SET permanent_404 = TRUE,
                jd_fetch_source = 'closed_at_source',
                jd_fetch_attempted_at = NOW()
            WHERE job_id = 'test-job-relist'
            """
        )
        conn.commit()

    # Same job_id re-listed with a CHANGED hash (new title) = genuinely back.
    job_v2 = make_job(
        job_id="test-job-relist",
        company_name="RelistCo",
        role_title="Senior SWE",
        content_hash=compute_content_hash("RelistCo", "Senior SWE"),
    )
    with _connect() as conn:
        upsert_jobs([job_v2], conn)

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT permanent_404, jd_fetch_source, jd_fetch_attempted_at, status
            FROM jobs WHERE job_id = 'test-job-relist'
            """
        ).fetchone()

    assert row["permanent_404"] is False, "permanent_404 must reset on re-list"
    assert row["jd_fetch_source"] is None, "terminal source sentinel must clear"
    assert row["jd_fetch_attempted_at"] is None, "attempt timestamp must clear so Stage 2b re-admits"
    assert row["status"] == "active"


def test_upsert_unchanged_hash_keeps_permanent_404():
    """A re-seen tombstone with the SAME hash (not genuinely re-listed) must KEEP
    permanent_404 — only a content change signals a real re-list."""
    job = make_job(
        job_id="test-job-stilldead",
        company_name="StillDeadCo",
        role_title="Closed Role",
        content_hash=compute_content_hash("StillDeadCo", "Closed Role"),
    )
    with _connect() as conn:
        upsert_jobs([job], conn)
        conn.execute(
            "UPDATE jobs SET permanent_404 = TRUE, jd_fetch_source = 'closed_at_source' "
            "WHERE job_id = 'test-job-stilldead'"
        )
        conn.commit()

    # Re-seen, SAME hash.
    with _connect() as conn:
        upsert_jobs([job], conn)

    with _connect() as conn:
        row = conn.execute(
            "SELECT permanent_404, jd_fetch_source FROM jobs WHERE job_id = 'test-job-stilldead'"
        ).fetchone()

    assert row["permanent_404"] is True, "unchanged-hash tombstone must persist"
    assert row["jd_fetch_source"] == "closed_at_source"


def test_mark_stale_jobs_marks_missing_ids_inactive():
    """Test 5: mark_stale_jobs sets status='inactive' for rows NOT in seen_ids,
    while rows IN seen_ids remain status='active'."""
    # Use a DEDICATED test repo (not a real one) so the partial-scrape circuit
    # breaker — which trips when a run would deactivate >50% of a repo's active
    # rows above a 20-row floor — sees only these test rows and stays out of the
    # way (3 active < 20 floor). force=True also bypasses it; here the floor is
    # the natural guard for a tiny isolated repo.
    test_repo = "test-repo-stale-5"
    job_a = make_job(job_id="test-job-a", company_name="CompanyA", source_repo=test_repo)
    job_b = make_job(job_id="test-job-b", company_name="CompanyB", source_repo=test_repo)
    job_c = make_job(job_id="test-job-c", company_name="CompanyC", source_repo=test_repo)

    with _connect() as conn:
        upsert_jobs([job_a, job_b, job_c], conn)

    # Simulate: only job_a is in the latest scrape; job_b and job_c have disappeared
    seen_ids = {"test-job-a"}
    with _connect() as conn:
        stale_count = mark_stale_jobs(seen_ids, test_repo, conn)

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
    repo_a = "test-repo-intern-6"
    repo_b = "test-repo-newgrad-6"
    internship_job = make_job(
        job_id="test-job-intern",
        company_name="InternCo",
        source_repo=repo_a,
    )
    newgrad_job = make_job(
        job_id="test-job-newgrad",
        company_name="NewGradCo",
        source_repo=repo_b,
    )

    with _connect() as conn:
        upsert_jobs([internship_job, newgrad_job], conn)

    # Mark stale for repo_a only (seen_ids empty — its 1 row goes stale; <20-row
    # floor keeps the circuit-breaker out of the way for a tiny isolated repo).
    with _connect() as conn:
        stale_count = mark_stale_jobs(set(), repo_a, conn)

    assert stale_count == 1, f"Expected 1 stale row (internship only), got {stale_count}"

    with _connect() as conn:
        intern_row = conn.execute(
            "SELECT status FROM jobs WHERE job_id = 'test-job-intern'"
        ).fetchone()
        newgrad_row = conn.execute(
            "SELECT status FROM jobs WHERE job_id = 'test-job-newgrad'"
        ).fetchone()

    assert intern_row["status"] == "inactive", "Internship job should be inactive"
    assert newgrad_row["status"] == "active", (
        "New-grad job must NOT be affected by internships stale run"
    )
