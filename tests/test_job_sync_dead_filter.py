"""Regression: the Firestore active-sync query must EXCLUDE dead / permanent_404
jobs.

Live audit 2026-05-29 found 1,792 jobs with dead=true sitting in the matchable
set (status='active' + embedding + JD + skills) — a liveness sweep set dead=true
without flipping status, so confirmed-dead postings synced to Firestore and were
served as live matches ("click a match, the job is gone"). _fetch_active_jobs now
filters dead/permanent_404 at the sync boundary; these tests pin that contract.

Pure-mock: captures the SQL handed to conn.execute(), no DB required.
"""
from unittest.mock import MagicMock

from wekruit_matching.pipeline.job_sync import _fetch_active_jobs


def _captured_active_sql() -> str:
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = []
    conn.execute.return_value = cur
    _fetch_active_jobs(conn, since=None)
    assert conn.execute.called, "_fetch_active_jobs must issue a query"
    return conn.execute.call_args[0][0]


def _normalize(sql: str) -> str:
    """Strip the ``j.`` table alias (fix #4 LEFT JOINs the synced-hash ledger
    and aliases jobs AS j) so these contract assertions are alias-agnostic."""
    return sql.replace("j.", "")


def test_active_sync_excludes_dead():
    sql = _normalize(_captured_active_sql())
    assert "COALESCE(dead, FALSE) = FALSE" in sql, (
        "dead=true jobs must be excluded from the Firestore active-sync set"
    )


def test_active_sync_excludes_permanent_404():
    sql = _normalize(_captured_active_sql())
    assert "COALESCE(permanent_404, FALSE) = FALSE" in sql, (
        "permanent_404 jobs must be excluded from the Firestore active-sync set"
    )


def test_active_sync_preserves_existing_gates():
    # The dead/404 filter must be ADDED, not replace the Track-D quality gate.
    sql = _normalize(_captured_active_sql())
    assert "status = 'active'" in sql
    assert "embedding IS NOT NULL" in sql
    assert "embedded_at IS NOT NULL" in sql
    assert "length(job_description) >= 200" in sql
    assert "cardinality(required_skills) > 0" in sql
