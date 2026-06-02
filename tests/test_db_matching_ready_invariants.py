"""Integration tests for the matching-ready CHECK constraints (alembic 0010,
reliability audit rank 2). These require a live Postgres with migration 0010
applied; they skip otherwise.

Each test attempts to write a row in a STAMP_WITHOUT_VERIFY corruption shape and
asserts Postgres rejects it with a CheckViolation — i.e. the bug class is
structurally unrepresentable, not merely guarded in app code.
"""
from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.rows import dict_row


def _connect():
    url = os.environ.get("DATABASE_URL", "").replace("postgresql+psycopg://", "postgresql://", 1)
    if not url or url == "postgresql://":
        pytest.skip("DATABASE_URL not set — skipping DB constraint tests")
    try:
        conn = psycopg.connect(url, row_factory=dict_row)
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Cannot connect to DB: {e}")
    # Skip if 0010 not applied.
    n = conn.execute(
        "SELECT count(*) AS n FROM pg_constraint WHERE conname = 'ck_enriched_requires_skills_or_no_jd'"
    ).fetchone()["n"]
    if not n:
        conn.close()
        pytest.skip("migration 0010 not applied — constraints absent")
    return conn


@pytest.fixture(autouse=True)
def _cleanup():
    conn = _connect()
    conn.execute("DELETE FROM jobs WHERE job_id LIKE 'ckinv-%'")
    conn.commit()
    yield
    conn.execute("DELETE FROM jobs WHERE job_id LIKE 'ckinv-%'")
    conn.commit()
    conn.close()


def _insert(conn, **over):
    cols = {
        "job_id": "ckinv-1",
        "source_repo": "test",
        "company_name": "X",
        "role_title": "Y",
        "content_hash": "h",
        "status": "active",
    }
    cols.update(over)
    keys = ", ".join(cols)
    ph = ", ".join(f"%({k})s" for k in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({ph})", cols)
    conn.commit()


def test_enriched_with_jd_but_no_skills_is_rejected():
    """c1: enriched_at + usable JD + empty skills -> the original lockout shape."""
    with _connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(
                conn,
                job_description="x" * 250,
                required_skills=[],
                enriched_at="now()",
            )


def test_enriched_no_jd_no_skills_is_allowed():
    """c1 spares the genuine empty-at-source floor: enriched, NO usable JD,
    no skills is a legit 'we tried, nothing to extract' state."""
    with _connect() as conn:
        # NULL JD -> allowed even with enriched_at set + empty skills.
        _insert(conn, job_id="ckinv-floor", required_skills=[], enriched_at="now()")
        row = conn.execute(
            "SELECT job_id FROM jobs WHERE job_id = 'ckinv-floor'"
        ).fetchone()
        assert row is not None


def test_embedded_without_vector_is_rejected():
    """c2: embedded_at set with NULL embedding."""
    with _connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(
                conn,
                job_description="x" * 250,
                required_skills=["python"],
                embedded_at="now()",
            )


def test_jd_source_on_thin_jd_is_rejected():
    """c4: a real ATS source name stamped on a sub-200 JD."""
    with _connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert(
                conn,
                job_description="too short",
                jd_fetch_source="greenhouse",
            )


def test_jd_source_failed_on_thin_jd_is_allowed():
    """c4 allows the terminal sentinels on a thin/empty JD (that's their job)."""
    with _connect() as conn:
        _insert(conn, job_id="ckinv-failed", job_description=None, jd_fetch_source="failed")
        row = conn.execute("SELECT job_id FROM jobs WHERE job_id = 'ckinv-failed'").fetchone()
        assert row is not None
