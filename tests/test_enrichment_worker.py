"""Integration tests for the enrichment worker.

Skip all tests when DATABASE_URL is not set (same pattern as test_scraper_upsert.py).
All classify_job calls are mocked — no real Anthropic API key needed.
"""
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

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
