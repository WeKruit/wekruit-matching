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


# NOTE (2026-05-31): these worker tests use a mock connection (see _make_mock_conn
# below), NOT a live DB. The previous versions ran embed_pending() against the real
# prod database: embed_pending's eligibility SELECT does `... WHERE embedded_at IS NULL
# ... LIMIT 3000` with no test scoping, so it grabbed the whole prod backlog and
# committed real embeddings as a side effect of running the test. The mock approach is
# deterministic and never touches prod. SQL-gate semantics are checked by asserting on
# the SELECT text (the gate must screen on JD+skills, and must NOT require enriched_at).


def _captured_sql(conn):
    """All SQL strings embed_pending passed to conn.execute (mock)."""
    return [c.args[0] for c in conn.execute.call_args_list if c.args]


def test_embed_gate_screens_on_jd_and_skills_not_enriched_at():
    """The eligibility SELECT must gate on JD>=200 + skills>0 and must NOT require enriched_at.

    Regression for the 2026-05-31 lockout fix: requiring `enriched_at IS NOT NULL`
    stranded ~18k churned-but-complete jobs (upsert nulls enriched_at while preserving
    JD+skills; gap-fill enrichers skip rows that already have JD+skills).
    """
    from wekruit_matching.embedding.worker import embed_pending

    conn = _make_mock_conn([])  # no eligible rows -> worker returns early, we inspect SQL
    with patch("wekruit_matching.embedding.worker.register_vector"), patch(
        "wekruit_matching.embedding.worker.assert_embedding_model_consistency"
    ):
        embed_pending(conn)

    sel = next(
        s for s in _captured_sql(conn)
        if "FROM jobs" in s and "embedded_at IS NULL" in s
    )
    assert "enriched_at" not in sel, "embed gate must NOT require enriched_at (lockout fix)"
    assert "length(job_description) >= 200" in sel, "must keep the JD>=200 quality gate"
    assert "cardinality(required_skills) > 0" in sel, "must keep the skills gate"
    assert "status = 'active'" in sel


def test_embed_pending_embeds_churned_job_with_jd_and_skills():
    """A churned job (JD+skills present, enriched_at=None) MUST be embedded.

    This is the row shape that was permanently stranded before the fix. The eligibility
    SELECT (real SQL) already filters by JD+skills; here the mock returns one such row
    and we assert the worker embeds it (enriched_at is irrelevant to the worker now).
    """
    from wekruit_matching.embedding.worker import embed_pending

    conn = _make_mock_conn([_job_row("churned-1", skills=["python", "sql"])])
    with patch("wekruit_matching.embedding.worker.register_vector"), patch(
        "wekruit_matching.embedding.worker.assert_embedding_model_consistency"
    ), patch(
        "wekruit_matching.embedding.worker.embed_texts", return_value=[[0.1] * 1536]
    ), patch(
        "wekruit_matching.embedding.worker.embed_text", return_value=[0.1] * 1536
    ):
        result = embed_pending(conn)

    assert result["embedded"] == 1, result
    assert result["failed"] == 0, result


def test_embed_pending_no_eligible_rows_is_noop():
    """When the SELECT returns nothing, the worker embeds nothing (no crash)."""
    from wekruit_matching.embedding.worker import embed_pending

    conn = _make_mock_conn([])
    with patch("wekruit_matching.embedding.worker.register_vector"), patch(
        "wekruit_matching.embedding.worker.assert_embedding_model_consistency"
    ), patch(
        "wekruit_matching.embedding.worker.embed_texts", return_value=[]
    ) as mock_batch:
        result = embed_pending(conn)

    assert result["embedded"] == 0, result
    assert mock_batch.call_count == 0


def test_embed_pending_continues_after_failure():
    """Per-job isolation: an embed_text() exception for one job does not abort the batch.

    Uses the per-job fallback path (batch raises -> per-job), so the middle job's
    failure is isolated and the other two still embed.
    """
    from wekruit_matching.embedding.worker import embed_pending

    conn = _make_mock_conn([_job_row("j1"), _job_row("j2"), _job_row("j3")])
    call_count = [0]

    def per_job(text, client=None):
        call_count[0] += 1
        if call_count[0] == 2:
            raise RuntimeError("simulated embed_text failure")
        return [0.1] * 1536

    with patch("wekruit_matching.embedding.worker.register_vector"), patch(
        "wekruit_matching.embedding.worker.assert_embedding_model_consistency"
    ), patch(
        # force the per-job path so each job is isolated
        "wekruit_matching.embedding.worker.embed_texts",
        side_effect=RuntimeError("batch down"),
    ), patch(
        "wekruit_matching.embedding.worker.embed_text", side_effect=per_job
    ):
        result = embed_pending(conn)

    assert result["embedded"] == 2, result
    assert result["failed"] == 1, result


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
