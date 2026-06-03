"""Regression: the Serper ATS resolver (_flush) must never write a value that
violates the alembic-0010 matching-ready CHECK constraints.

2026-06-03 incident: Stage 2.5 (ats_resolve) crashed in the live nightly with
``new row for relation "jobs" violates check constraint
"ck_jd_source_requires_usable_jd"`` because ``_flush`` stamped
``jd_fetch_source='serper_miss'`` (and ``'serper'`` on hits) on rows with no
usable JD — neither is in the constraint's allow-list
(``failed`` / ``skip_no_url`` / ``closed_at_source``). The resolver also bumped
``embedded_at=now()`` on hits, which violates ``ck_embedded_requires_vector``
when the row is not yet embedded.

Fix: misses -> ``jd_fetch_source='skip_no_url'`` (a legal "no URL" sentinel);
hits stamp NO jd_fetch_source (resolving a URL is not fetching a JD) and bump
``embedded_at`` only when ``embedding IS NOT NULL``.

@integration: needs a live *_test Postgres (WS-A ``pg_conn`` fixture). Runs in
the CI gate, so this seam can never silently regress again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from psycopg.rows import dict_row

# scripts/ is imported by pipeline.daily at runtime; ensure the repo root is on
# sys.path so the same import resolves under pytest regardless of import mode.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.resolve_jobright_ats import _flush  # noqa: E402

pytestmark = pytest.mark.integration

_VECTOR = "[" + ",".join(["0.01"] * 1536) + "]"


def _insert(conn, job_id, **over):
    cols = {
        "job_id": job_id,
        "source_repo": "jobright-newgrad",
        "company_name": "Acme",
        "role_title": "Engineer",
        "primary_url": "https://jobright.ai/jobs/info/abc",
        "status": "active",
        **over,
    }
    keys = ", ".join(cols)
    ph = ", ".join(f"%({k})s" for k in cols)
    conn.execute(f"INSERT INTO jobs ({keys}) VALUES ({ph})", cols)


def _cleanup(conn, *job_ids):
    conn.execute("DELETE FROM jobs WHERE job_id = ANY(%(ids)s)", {"ids": list(job_ids)})
    conn.commit()


def test_miss_writes_skip_no_url_not_serper_miss(pg_conn):
    """A miss on a row with NO usable JD must stamp 'skip_no_url' and NOT raise a
    CheckViolation (the 2026-06-03 crash)."""
    conn = pg_conn
    conn.row_factory = dict_row
    jid = "test-resolve-miss-thinjd"
    _cleanup(conn, jid)
    # No JD, no embedding -> the exact shape that crashed with 'serper_miss'.
    _insert(conn, jid, job_description=None)
    conn.commit()
    try:
        _flush(conn, updates=[], misses=[jid])  # must NOT raise
        row = conn.execute(
            "SELECT jd_fetch_source FROM jobs WHERE job_id = %(j)s", {"j": jid}
        ).fetchone()
        assert row["jd_fetch_source"] == "skip_no_url", row
    finally:
        _cleanup(conn, jid)


def test_hit_on_unembedded_row_is_constraint_safe(pg_conn):
    """A hit on an unembedded row must set ats_apply_url, must NOT stamp a
    JD-source on a thin JD, and must NOT bump embedded_at (no embedding)."""
    conn = pg_conn
    conn.row_factory = dict_row
    jid = "test-resolve-hit-unembedded"
    _cleanup(conn, jid)
    _insert(conn, jid, job_description=None)  # unembedded, no JD
    conn.commit()
    try:
        _flush(conn, updates=[("https://boards.greenhouse.io/acme/jobs/1", jid)], misses=[])
        row = conn.execute(
            "SELECT ats_apply_url, jd_fetch_source, embedded_at, embedding "
            "FROM jobs WHERE job_id = %(j)s",
            {"j": jid},
        ).fetchone()
        assert row["ats_apply_url"] == "https://boards.greenhouse.io/acme/jobs/1", row
        assert row["jd_fetch_source"] != "serper", row  # no JD-source claim
        assert row["embedding"] is None and row["embedded_at"] is None, row  # not bumped
    finally:
        _cleanup(conn, jid)


def test_hit_on_embedded_row_bumps_watermark(pg_conn):
    """A hit on an ALREADY-embedded row still bumps embedded_at (the sync
    re-selection watermark) — without violating ck_embedded_requires_vector."""
    conn = pg_conn
    conn.row_factory = dict_row
    jid = "test-resolve-hit-embedded"
    _cleanup(conn, jid)
    _insert(
        conn,
        jid,
        job_description="x" * 300,
        required_skills=["python"],
        enriched_at="now()",
        embedding=_VECTOR,
        embedding_model="text-embedding-3-small",
        embedded_at="now()",
    )
    # Move embedded_at into the past via a SQL expression — a bare param string
    # "now() - interval '2 days'" is not a valid timestamptz literal.
    conn.execute(
        "UPDATE jobs SET embedded_at = now() - interval '2 days' WHERE job_id = %(j)s",
        {"j": jid},
    )
    conn.commit()
    old = conn.execute(
        "SELECT embedded_at FROM jobs WHERE job_id = %(j)s", {"j": jid}
    ).fetchone()["embedded_at"]
    try:
        _flush(conn, updates=[("https://jobs.lever.co/acme/2", jid)], misses=[])
        row = conn.execute(
            "SELECT ats_apply_url, embedded_at FROM jobs WHERE job_id = %(j)s",
            {"j": jid},
        ).fetchone()
        assert row["ats_apply_url"] == "https://jobs.lever.co/acme/2", row
        assert row["embedded_at"] > old, (row["embedded_at"], old)  # watermark bumped
    finally:
        _cleanup(conn, jid)
