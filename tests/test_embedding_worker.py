"""Integration tests for the embedding worker.

Skip all tests when DATABASE_URL is not set (same pattern as test_enrichment_worker.py).
All embed_text calls are mocked — no real OpenAI API key needed.
"""
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

DATABASE_URL = os.getenv("DATABASE_URL")
skip_no_db = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL not set — skipping DB integration tests",
)


def _connect():
    from wekruit_matching.db.connection import get_connection
    return get_connection


def _insert_job(
    conn,
    job_id: str,
    embedded_at=None,
    enriched_at=None,
    content_hash: str = "a" * 64,
):
    now = datetime.now(timezone.utc)
    _enriched_at = enriched_at if enriched_at is not None else now
    conn.execute(
        """
        INSERT INTO jobs (job_id, source_repo, company_name, role_title, location_raw,
                          status, content_hash, required_skills, enriched_at, embedded_at)
        VALUES (%(job_id)s, 'Summer2026-Internships', 'TestCo', 'SWE Intern', 'SF',
                'active', %(content_hash)s, ARRAY[]::text[], %(enriched_at)s, %(embedded_at)s)
        ON CONFLICT (job_id) DO NOTHING
        """,
        {
            "job_id": job_id,
            "content_hash": content_hash,
            "enriched_at": _enriched_at,
            "embedded_at": embedded_at,
        },
    )
    conn.commit()


def _insert_job_unenriched(conn, job_id: str, content_hash: str = "a" * 64):
    """Insert a job with enriched_at=None (not enriched, should be skipped by embedder)."""
    conn.execute(
        """
        INSERT INTO jobs (job_id, source_repo, company_name, role_title, location_raw,
                          status, content_hash, required_skills, enriched_at, embedded_at)
        VALUES (%(job_id)s, 'Summer2026-Internships', 'TestCo', 'SWE Intern', 'SF',
                'active', %(content_hash)s, ARRAY[]::text[], NULL, NULL)
        ON CONFLICT (job_id) DO NOTHING
        """,
        {"job_id": job_id, "content_hash": content_hash},
    )
    conn.commit()


def _cleanup(conn, *job_ids):
    conn.execute("DELETE FROM jobs WHERE job_id = ANY(%(ids)s)", {"ids": list(job_ids)})
    conn.commit()


@skip_no_db
def test_embed_pending_skips_already_embedded():
    """Jobs with embedded_at already set must not be re-embedded (skip via SQL gate)."""
    from wekruit_matching.embedding.worker import embed_pending

    job_id = "1" * 64
    now = datetime.now(timezone.utc)
    get_conn = _connect()

    with get_conn() as conn:
        _insert_job(conn, job_id, embedded_at=now)
        try:
            with patch("wekruit_matching.embedding.worker.embed_text") as mock_embed:
                mock_embed.return_value = [0.5] * 1536
                result = embed_pending(conn)
            assert mock_embed.call_count == 0, (
                "embed_text must not be called for already-embedded jobs"
            )
            assert result["embedded"] == 0
        finally:
            _cleanup(conn, job_id)


@skip_no_db
def test_embed_pending_skips_unenriched():
    """Jobs with enriched_at=None must not appear in the embedding queue."""
    from wekruit_matching.embedding.worker import embed_pending

    job_id = "2" * 64
    get_conn = _connect()

    with get_conn() as conn:
        _insert_job_unenriched(conn, job_id)
        try:
            with patch("wekruit_matching.embedding.worker.embed_text") as mock_embed:
                mock_embed.return_value = [0.5] * 1536
                result = embed_pending(conn)
            assert mock_embed.call_count == 0, (
                "embed_text must not be called for unenriched jobs"
            )
            assert result["embedded"] == 0
        finally:
            _cleanup(conn, job_id)


@skip_no_db
def test_embed_pending_embeds_enriched_job():
    """Jobs with enriched_at set and embedded_at=None must be embedded."""
    from wekruit_matching.embedding.worker import embed_pending

    job_id = "3" * 64
    get_conn = _connect()

    with get_conn() as conn:
        _insert_job(conn, job_id, embedded_at=None)
        try:
            with patch("wekruit_matching.embedding.worker.embed_text") as mock_embed:
                mock_embed.return_value = [0.5] * 1536
                result = embed_pending(conn)

            assert result["embedded"] >= 1, f"Expected at least 1 embedded, got {result}"
            row = conn.execute(
                "SELECT embedding_model, embedded_at FROM jobs WHERE job_id = %(id)s",
                {"id": job_id},
            ).fetchone()
            assert row["embedding_model"] == "text-embedding-3-small"
            assert row["embedded_at"] is not None
        finally:
            _cleanup(conn, job_id)


@skip_no_db
def test_embed_pending_continues_after_failure():
    """Per-job isolation: embed_text() exception for one job does not abort the batch."""
    from wekruit_matching.embedding.worker import embed_pending

    job_ids = ["4" * 64, "5" * 64, "6" * 64]
    get_conn = _connect()

    with get_conn() as conn:
        for jid in job_ids:
            _insert_job(conn, jid, embedded_at=None)
        try:
            call_count = [0]

            def side_effect(text, client=None):
                call_count[0] += 1
                if call_count[0] == 2:
                    raise RuntimeError("simulated embed_text failure")
                return [0.5] * 1536

            with patch("wekruit_matching.embedding.worker.embed_text", side_effect=side_effect):
                result = embed_pending(conn)

            assert result["embedded"] == 2, f"Expected 2 embedded, got {result}"
            assert result["failed"] == 1, f"Expected 1 failed, got {result}"
        finally:
            _cleanup(conn, *job_ids)


@skip_no_db
def test_hnsw_index_used_for_cosine_query():
    """Verify EXPLAIN ANALYZE shows HNSW index scan for cosine similarity query.

    Uses SET enable_seqscan=OFF to force index usage on small tables
    (planner prefers seqscan for small tables; disable to verify index exists).
    Pattern established in Phase 1 (test_db_schema.py).
    """
    from wekruit_matching.db.connection import get_connection
    from pgvector.psycopg import register_vector

    query_vector = [0.1] * 1536

    get_conn = get_connection
    with get_conn() as conn:
        register_vector(conn)
        conn.execute("SET enable_seqscan = OFF")
        explain_result = conn.execute(
            """
            EXPLAIN ANALYZE
            SELECT job_id, embedding <=> %(v)s AS distance
            FROM jobs
            ORDER BY embedding <=> %(v)s
            LIMIT 5
            """,
            {"v": query_vector},
        ).fetchall()

    plan_text = " ".join(str(row) for row in explain_result)
    assert "hnsw" in plan_text.lower() or "index" in plan_text.lower(), (
        f"Expected HNSW index scan in query plan. Got: {plan_text}"
    )


def _job_row(job_id: str, skills=None):
    """Minimal jobs-table row dict as embed_pending() reads it (dict access)."""
    return {
        "job_id": job_id,
        "source_repo": "Summer2026-Internships",
        "company_name": "TestCo",
        "role_title": "SWE Intern",
        "location_raw": "SF",
        "required_skills": skills if skills is not None else ["python"],
        "content_hash": "a" * 64,
    }


def _make_mock_conn(job_rows):
    """Mock psycopg conn that feeds embed_pending() without a real DB.

    - model-consistency check (SELECT DISTINCT embedding_model) -> []
    - main eligibility SELECT (embedded_at IS NULL ...)         -> job_rows
    - per-job UPDATE writes                                     -> empty cursor
    """
    conn = MagicMock()

    def _execute(sql, params=None):
        cur = MagicMock()
        if "DISTINCT embedding_model" in sql:
            cur.fetchall.return_value = []
        elif "FROM jobs" in sql and "embedded_at IS NULL" in sql:
            cur.fetchall.return_value = job_rows
        else:
            cur.fetchall.return_value = []
            cur.fetchone.return_value = None
        return cur

    conn.execute.side_effect = _execute
    return conn


class TestEmbedPendingCountParity:
    """A short vector list from embed_texts must not silently truncate the batch;
    the worker recovers via per-job embedding so every job is still embedded 1:1."""

    def test_short_batch_response_falls_back_to_per_job(self) -> None:
        from wekruit_matching.embedding.worker import embed_pending

        conn = _make_mock_conn([_job_row("j1"), _job_row("j2"), _job_row("j3")])
        # 3 inputs but only 1 vector back -- the misalignment the guard catches.
        with patch("wekruit_matching.embedding.worker.register_vector"), patch(
            "wekruit_matching.embedding.worker.embed_texts",
            return_value=[[0.1] * 1536],
        ), patch(
            "wekruit_matching.embedding.worker.embed_text",
            return_value=[0.9] * 1536,
        ) as mock_embed_text:
            result = embed_pending(conn)

        assert result["embedded"] == 3, result
        assert result["failed"] == 0, result
        assert mock_embed_text.call_count == 3, (
            "each job must be re-embedded individually after the count mismatch"
        )
