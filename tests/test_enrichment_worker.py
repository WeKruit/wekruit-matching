"""Integration tests for the enrichment worker.

Skip all tests when DATABASE_URL is not set (same pattern as test_scraper_upsert.py).
All classify_job calls are mocked — no real Anthropic API key needed.
"""
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# _FakeConn helpers for pure unit tests (no DB required)
# ---------------------------------------------------------------------------

class _FakeResult:
    """Mimics psycopg cursor result with fetchall()."""

    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """Minimal fake psycopg3 connection that captures executed queries."""

    def __init__(self, rows=None):
        # rows returned by SELECT
        self._rows = rows or []
        self.executed = []  # list of (query, params) tuples

    def execute(self, query, params=None):
        self.executed.append((query, params))
        return _FakeResult(self._rows)

    def commit(self):
        pass

    def rollback(self):
        pass


# ---------------------------------------------------------------------------
# ENRICH-01 gap-fill gate — unit tests (no DB needed, use _FakeConn)
# ---------------------------------------------------------------------------

def test_enrich_pending_where_clause_contains_gap_fill_condition():
    """WHERE clause in enrich_pending must gate on missing JD or empty skills."""
    from wekruit_matching.enrichment.worker import enrich_pending

    conn = _FakeConn(rows=[])  # no rows — just check the query
    enrich_pending(conn)

    assert conn.executed, "enrich_pending must execute at least one query"
    select_query = conn.executed[0][0]
    # Must contain the ENRICH-01 gap-fill condition
    assert "required_skills" in select_query, (
        "WHERE clause must reference required_skills for ENRICH-01 gap-fill gate"
    )


def test_enrich_pending_where_clause_contains_null_jd_check():
    """WHERE clause must include job_description IS NULL check."""
    from wekruit_matching.enrichment.worker import enrich_pending

    conn = _FakeConn(rows=[])
    enrich_pending(conn)

    select_query = conn.executed[0][0]
    assert "job_description IS NULL" in select_query, (
        "WHERE clause must include 'job_description IS NULL' for ENRICH-01"
    )


def test_enrich_pending_log_message_mentions_gap_fill(capfd):
    """Log message must mention 'gap-fill' when jobs are found."""
    from wekruit_matching.enrichment.worker import enrich_pending
    from wekruit_matching.enrichment.classifier import EnrichmentResult
    import io
    from loguru import logger

    # Provide a row that would be returned by the gap-fill query
    fake_row = {
        "job_id": "a" * 64,
        "source_repo": "Summer2026-Internships",
        "company_name": "TestCo",
        "role_title": "SWE Intern",
        "location_raw": "SF",
        "content_hash": "b" * 64,
        "job_description": None,
        "required_skills": [],
    }
    conn = _FakeConn(rows=[fake_row])

    log_messages = []

    def sink(msg):
        log_messages.append(str(msg))

    logger.add(sink, level="INFO")
    try:
        with patch("wekruit_matching.enrichment.worker.classify_job") as mock_classify:
            mock_classify.return_value = EnrichmentResult(
                industry="tech", company_size="startup", required_skills=[], sponsorship=None
            )
            enrich_pending(conn)
    finally:
        logger.remove()

    combined = " ".join(log_messages)
    assert "gap-fill" in combined, (
        f"Log message must mention 'gap-fill' when jobs are found. Got: {combined}"
    )

DATABASE_URL = os.getenv("DATABASE_URL")
skip_no_db = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _connect():
    from wekruit_matching.db.connection import get_connection
    return get_connection


def _insert_job(conn, job_id: str, enriched_at=None, content_hash: str = "a" * 64):
    conn.execute(
        """
        INSERT INTO jobs (job_id, source_repo, company_name, role_title, location_raw,
                          status, content_hash, enriched_at)
        VALUES (%(job_id)s, 'Summer2026-Internships', 'TestCo', 'SWE Intern', 'SF',
                'active', %(content_hash)s, %(enriched_at)s)
        ON CONFLICT (job_id) DO NOTHING
        """,
        {"job_id": job_id, "content_hash": content_hash, "enriched_at": enriched_at},
    )
    conn.commit()


def _cleanup(conn, *job_ids):
    conn.execute("DELETE FROM jobs WHERE job_id = ANY(%(ids)s)", {"ids": list(job_ids)})
    conn.commit()


@skip_no_db
def test_enrich_pending_skips_already_enriched():
    from wekruit_matching.enrichment.worker import enrich_pending
    from wekruit_matching.enrichment.classifier import EnrichmentResult

    job_id = "b" * 64
    now = datetime.now(timezone.utc)
    get_conn = _connect()

    with get_conn() as conn:
        _insert_job(conn, job_id, enriched_at=now)
        try:
            with patch("wekruit_matching.enrichment.worker.classify_job") as mock_classify:
                mock_classify.return_value = EnrichmentResult(
                    industry="tech", company_size="startup", required_skills=[], sponsorship=None
                )
                result = enrich_pending(conn)
            assert mock_classify.call_count == 0, "classify_job must not be called for already-enriched jobs"
            assert result["enriched"] == 0
        finally:
            _cleanup(conn, job_id)


@skip_no_db
def test_enrich_pending_enriches_unenriched():
    from wekruit_matching.enrichment.worker import enrich_pending
    from wekruit_matching.enrichment.classifier import EnrichmentResult

    job_id = "c" * 64
    get_conn = _connect()

    with get_conn() as conn:
        _insert_job(conn, job_id, enriched_at=None)
        try:
            mock_result = EnrichmentResult(
                industry="fintech", company_size="midsize", required_skills=["python"], sponsorship=True
            )
            with patch("wekruit_matching.enrichment.worker.classify_job", return_value=mock_result):
                result = enrich_pending(conn)

            assert result["enriched"] >= 1
            row = conn.execute("SELECT * FROM jobs WHERE job_id = %(id)s", {"id": job_id}).fetchone()
            assert row["industry"] == "fintech"
            assert row["company_size"] == "midsize"
            assert "python" in row["required_skills"]
            assert row["sponsorship"] is True
            assert row["enriched_at"] is not None
        finally:
            _cleanup(conn, job_id)


@skip_no_db
def test_enrich_pending_continues_after_failure():
    from wekruit_matching.enrichment.worker import enrich_pending
    from wekruit_matching.enrichment.classifier import EnrichmentResult

    job_ids = ["d" * 64, "e" * 64, "f" * 64]
    get_conn = _connect()

    with get_conn() as conn:
        for jid in job_ids:
            _insert_job(conn, jid, enriched_at=None)
        try:
            good_result = EnrichmentResult(
                industry="tech", company_size="startup", required_skills=[], sponsorship=None
            )
            call_count = [0]
            def side_effect(job):
                call_count[0] += 1
                if call_count[0] == 2:
                    raise RuntimeError("simulated API failure")
                return good_result

            with patch("wekruit_matching.enrichment.worker.classify_job", side_effect=side_effect):
                result = enrich_pending(conn)

            assert result["enriched"] == 2
            assert result["failed"] == 1
        finally:
            _cleanup(conn, *job_ids)


@skip_no_db
def test_upsert_clears_enriched_at_on_hash_change():
    """Verify that upsert.py clears enriched_at when content_hash changes."""
    from wekruit_matching.scraper.upsert import upsert_jobs
    from wekruit_matching.models.job import Job
    from datetime import datetime, timezone

    job_id = "g" * 64
    get_conn = _connect()

    with get_conn() as conn:
        # Insert with enriched_at populated
        _insert_job(conn, job_id, enriched_at=datetime.now(timezone.utc), content_hash="a" * 64)
        conn.execute(
            "UPDATE jobs SET enriched_at = NOW() WHERE job_id = %(id)s", {"id": job_id}
        )
        conn.commit()

        # Verify enriched_at is set
        row_before = conn.execute("SELECT enriched_at FROM jobs WHERE job_id = %(id)s", {"id": job_id}).fetchone()
        assert row_before["enriched_at"] is not None

        try:
            # Upsert same job with different content_hash
            updated_job = Job(
                job_id=job_id,
                source_repo="Summer2026-Internships",
                company_name="TestCo",
                role_title="SWE Intern",
                location_raw="NYC",  # changed
                content_hash="b" * 64,  # different hash
            )
            upsert_jobs([updated_job], conn)

            row_after = conn.execute("SELECT enriched_at FROM jobs WHERE job_id = %(id)s", {"id": job_id}).fetchone()
            assert row_after["enriched_at"] is None, "enriched_at must be cleared when content_hash changes"
        finally:
            _cleanup(conn, job_id)


# ---------------------------------------------------------------------------
# P7-E (2026-05-08) — staleness gate tests
# Jobs with `enriched_at` stamped but still missing JD or skills must
# re-enter the queue once enriched_at < NOW() - INTERVAL '7 days'.
# ---------------------------------------------------------------------------

def test_enrich_pending_query_uses_staleness_window():
    """SELECT must use the ENRICH_STALE_DAYS interval to allow stuck jobs to retry."""
    from wekruit_matching.enrichment.worker import ENRICH_STALE_DAYS, enrich_pending

    conn = _FakeConn(rows=[])
    enrich_pending(conn)

    select_query = conn.executed[0][0]
    assert "enriched_at IS NULL" in select_query, (
        "Query must still allow never-enriched jobs (enriched_at IS NULL)"
    )
    assert f"INTERVAL '{ENRICH_STALE_DAYS} days'" in select_query, (
        "Query must use ENRICH_STALE_DAYS interval (P7-E gating fix)"
    )
    # The OR makes stuck jobs re-eligible. Critical: the disjunction must be inside
    # parentheses with the gap-fill predicate, not AND-mixed at the top level —
    # otherwise we'd reclassify already-fully-enriched stale jobs (which would
    # waste Qwen calls on rows that already have JD + skills).
    assert "OR enriched_at < NOW() - INTERVAL" in select_query, (
        "Query must use OR-form to combine enriched_at IS NULL and staleness check"
    )


def test_enrich_pending_query_keeps_data_gap_predicate():
    """Even with staleness retry, fully-enriched jobs must be excluded — they have
    JD + skills, so the data-gap predicate must remain a separate AND clause."""
    from wekruit_matching.enrichment.worker import enrich_pending

    conn = _FakeConn(rows=[])
    enrich_pending(conn)

    select_query = conn.executed[0][0]
    # Must AND the data-gap clause separately so a job with JD+skills is never
    # re-classified just because it's old.
    assert "job_description IS NULL" in select_query
    assert "required_skills = ARRAY[]::text[]" in select_query


@skip_no_db
def test_enrich_pending_requeues_stale_stuck_job():
    """Job with enriched_at = 8 days ago AND missing JD must be re-classified.

    This is the P7-E gating fix: previously such jobs were stuck because the
    SELECT only checked enriched_at IS NULL. Now they re-enter after 7 days.
    """
    from datetime import timedelta
    from wekruit_matching.enrichment.worker import enrich_pending
    from wekruit_matching.enrichment.classifier import EnrichmentResult

    job_id = "9" * 64
    eight_days_ago = datetime.now(timezone.utc) - timedelta(days=8)
    get_conn = _connect()

    with get_conn() as conn:
        # Insert with enriched_at = 8 days ago, no JD (the stuck pattern).
        # _insert_job's INSERT doesn't set job_description — it stays NULL,
        # which is exactly the stuck-job shape we want.
        _insert_job(conn, job_id, enriched_at=eight_days_ago, content_hash="9" * 64)
        try:
            mock_result = EnrichmentResult(
                industry="tech", company_size="midsize",
                required_skills=["python"], sponsorship=None,
            )
            with patch(
                "wekruit_matching.enrichment.worker.classify_job",
                return_value=mock_result,
            ) as mock_classify:
                # Limit query to 500 rows; our test job is among the oldest by
                # first_seen_at default, so it should be picked up.
                result = enrich_pending(conn)

            # The stuck job should have been re-classified at least once.
            assert any(
                call.args[0].job_id == job_id for call in mock_classify.call_args_list
            ), "Stuck job (enriched_at=8d ago, no JD) must be re-classified"
            assert result["enriched"] >= 1
            row = conn.execute(
                "SELECT enriched_at, required_skills FROM jobs WHERE job_id = %(id)s",
                {"id": job_id},
            ).fetchone()
            assert "python" in row["required_skills"]
            # enriched_at must have been refreshed to ~now (within last minute).
            assert (
                datetime.now(timezone.utc) - row["enriched_at"]
            ).total_seconds() < 120, "enriched_at must be refreshed on re-classification"
        finally:
            _cleanup(conn, job_id)


@skip_no_db
def test_enrich_pending_skips_recently_enriched_stuck_job():
    """Job with enriched_at = 1 day ago and missing JD must NOT be re-classified.

    Inverse of the above test — the staleness window protects against
    re-burning LLM calls when Stage 2b just hasn't caught up yet.
    """
    from datetime import timedelta
    from wekruit_matching.enrichment.worker import enrich_pending
    from wekruit_matching.enrichment.classifier import EnrichmentResult

    job_id = "8" * 64
    one_day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    get_conn = _connect()

    with get_conn() as conn:
        _insert_job(conn, job_id, enriched_at=one_day_ago, content_hash="8" * 64)
        try:
            mock_result = EnrichmentResult(
                industry="tech", company_size="startup",
                required_skills=[], sponsorship=None,
            )
            with patch(
                "wekruit_matching.enrichment.worker.classify_job",
                return_value=mock_result,
            ) as mock_classify:
                enrich_pending(conn)

            # The 1-day-old job must NOT be among the calls.
            called_ids = [c.args[0].job_id for c in mock_classify.call_args_list]
            assert job_id not in called_ids, (
                "Job enriched 1 day ago must NOT be re-classified (within staleness window)"
            )
        finally:
            _cleanup(conn, job_id)
