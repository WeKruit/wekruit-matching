"""Schema smoke tests — verify the DB is correctly set up (FOUND-02, FOUND-03, FOUND-04, FOUND-07).

These tests require a live Postgres instance with pgvector installed.
Run with: uv run pytest tests/test_db_schema.py -v

They are integration tests — they connect to the DATABASE_URL in .env.
Skip if DATABASE_URL is not set or points to a non-existent DB.
"""
import os
import hashlib
import pytest
import psycopg
from psycopg.rows import dict_row


def get_conninfo() -> str:
    """Convert SQLAlchemy URL to libpq format."""
    url = os.environ.get("DATABASE_URL", "")
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _connect():
    """Return a psycopg3 connection or skip if DB is unavailable."""
    conninfo = get_conninfo()
    if not conninfo or conninfo == "postgresql://":
        pytest.skip("DATABASE_URL not set — skipping DB schema tests")
    try:
        return psycopg.connect(conninfo, row_factory=dict_row)
    except Exception as e:
        pytest.skip(f"Cannot connect to DB: {e}")


def test_tables_exist():
    """jobs, user_profiles, feedback tables must exist after migration."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
        table_names = {r["tablename"] for r in rows}
    assert "jobs" in table_names, f"jobs table missing. Found: {table_names}"
    assert "user_profiles" in table_names, f"user_profiles table missing"
    assert "feedback" in table_names, f"feedback table missing"


def test_embedding_column_exists():
    """jobs.embedding column must exist with pgvector type."""
    with _connect() as conn:
        row = conn.execute("""
            SELECT data_type, udt_name
            FROM information_schema.columns
            WHERE table_name = 'jobs' AND column_name = 'embedding'
        """).fetchone()
    assert row is not None, "jobs.embedding column not found"
    # pgvector columns show as 'USER-DEFINED' in information_schema
    assert row["data_type"] in ("USER-DEFINED", "user-defined"), (
        f"Expected USER-DEFINED type, got: {row['data_type']}"
    )


def test_hnsw_index_exists():
    """HNSW index on jobs.embedding must exist."""
    with _connect() as conn:
        row = conn.execute("""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'jobs' AND indexname = 'ix_jobs_embedding_hnsw'
        """).fetchone()
    assert row is not None, "HNSW index ix_jobs_embedding_hnsw not found"
    assert "hnsw" in row["indexdef"].lower(), f"Index is not HNSW type: {row['indexdef']}"
    assert "vector_cosine_ops" in row["indexdef"], (
        f"Index missing vector_cosine_ops: {row['indexdef']}"
    )


def test_insert_job_succeeds():
    """INSERT into jobs with minimal fields must succeed."""
    job_id = hashlib.sha256(b"test-job").hexdigest()
    with _connect() as conn:
        # Clean up any previous test run
        conn.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
        conn.execute("""
            INSERT INTO jobs (job_id, source_repo, company_name, role_title)
            VALUES (%s, %s, %s, %s)
        """, (job_id, "Summer2026-Internships", "Test Corp", "SWE Intern"))
        conn.commit()
        row = conn.execute(
            "SELECT job_id, status FROM jobs WHERE job_id = %s", (job_id,)
        ).fetchone()
        # Clean up
        conn.execute("DELETE FROM jobs WHERE job_id = %s", (job_id,))
        conn.commit()
    assert row is not None
    assert row["job_id"] == job_id
    assert row["status"] == "active"


def test_cosine_query_uses_index():
    """EXPLAIN ANALYZE on cosine similarity query must show Index Scan, not Seq Scan.

    Inserts 15 dummy embeddings then runs a cosine similarity search.
    With fewer than ~10 rows, pgvector may choose seq scan regardless — hence 15 rows.
    """
    import numpy as np

    with _connect() as conn:
        # Register vector type
        from pgvector.psycopg import register_vector
        register_vector(conn)

        # Insert 15 dummy rows with random embeddings
        job_ids = []
        for i in range(15):
            jid = hashlib.sha256(f"dummy-job-{i}".encode()).hexdigest()
            job_ids.append(jid)
            embedding = np.random.rand(1536).astype(np.float32)
            conn.execute("""
                INSERT INTO jobs (job_id, source_repo, company_name, role_title, embedding, embedding_model)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (job_id) DO UPDATE SET embedding = EXCLUDED.embedding
            """, (jid, "test", f"Corp {i}", f"Role {i}", embedding, "text-embedding-3-small"))
        conn.commit()

        # Run EXPLAIN ANALYZE
        query_vec = np.random.rand(1536).astype(np.float32)
        plan = conn.execute("""
            EXPLAIN ANALYZE
            SELECT job_id, embedding <=> %s AS distance
            FROM jobs
            WHERE embedding IS NOT NULL
            ORDER BY distance
            LIMIT 10
        """, (query_vec,)).fetchall()

        # Clean up
        for jid in job_ids:
            conn.execute("DELETE FROM jobs WHERE job_id = %s", (jid,))
        conn.commit()

    plan_text = " ".join(str(r) for r in plan)
    assert "Seq Scan" not in plan_text, (
        f"Query used Seq Scan instead of HNSW index. Plan:\n{plan_text}"
    )
