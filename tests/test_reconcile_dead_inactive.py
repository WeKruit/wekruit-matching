"""Regression: reconcile_dead_inactive flips active dead/404 jobs to inactive.

Durable root-fix for the 2026-05-29 "dead jobs served to users" defect. Pure-mock:
captures the UPDATE SQL handed to conn.execute(), no DB required.
"""
from unittest.mock import MagicMock

from wekruit_matching.pipeline.dead_backfill import reconcile_dead_inactive


def _conn(rowcount: int = 0):
    conn = MagicMock()
    cur = MagicMock()
    cur.rowcount = rowcount
    conn.execute.return_value = cur
    return conn


def test_flips_active_dead_or_404_to_inactive():
    conn = _conn(rowcount=1893)
    n = reconcile_dead_inactive(conn)
    assert n == 1893
    sql = conn.execute.call_args[0][0]
    assert "SET status = 'inactive'" in sql
    assert "status = 'active'" in sql
    assert "COALESCE(dead, FALSE) = TRUE" in sql
    assert "COALESCE(permanent_404, FALSE) = TRUE" in sql
    conn.commit.assert_called_once()


def test_idempotent_zero_flip_is_clean():
    conn = _conn(rowcount=0)
    assert reconcile_dead_inactive(conn) == 0
    conn.commit.assert_called_once()  # commit even on no-op (harmless)


def test_only_touches_status_column():
    # The UPDATE must not modify dead/permanent_404/dead_confirmed_at — the
    # 90-day retry path depends on those flags surviving the status flip.
    conn = _conn(rowcount=5)
    reconcile_dead_inactive(conn)
    sql = conn.execute.call_args[0][0]
    set_clause = sql.split("WHERE")[0]
    assert "dead =" not in set_clause
    assert "dead_confirmed_at" not in set_clause
    assert "permanent_404 =" not in set_clause
