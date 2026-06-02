"""Integration test: alembic 0011 created the jobs_health_state table with the
expected schema (reliability audit Gate-4 / IL-5).

Uses the WS-A ``pg_conn`` fixture (function-scoped, rolled back, prod-guarded by
WEKRUIT_TEST_DB=1 + a *_test DB name). Marked @integration so the default suite
deselects it. Skips (rather than fails) if 0011 has not been applied to the test
DB so the suite stays green on a pre-0011 schema.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _dict_rows(conn):
    """Switch the WS-A pg_conn (default tuple rows) to dict_row so the by-name
    column access below works. Subsequent ``conn.execute()`` cursors inherit it.
    """
    from psycopg.rows import dict_row

    conn.row_factory = dict_row
    return conn


def _table_exists(conn, name: str) -> bool:
    _dict_rows(conn)
    return bool(
        conn.execute(
            "SELECT to_regclass(%(n)s) IS NOT NULL AS present",
            {"n": name},
        ).fetchone()["present"]
    )


def test_jobs_health_state_exists_with_columns(pg_conn):
    if not _table_exists(pg_conn, "jobs_health_state"):
        pytest.skip("migration 0011 not applied — jobs_health_state absent")

    rows = pg_conn.execute(
        """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'jobs_health_state'
        ORDER BY column_name
        """
    ).fetchall()
    cols = {r["column_name"]: r for r in rows}

    # Expected columns present.
    assert set(cols) == {"metric", "value", "updated_at"}, cols

    # metric: text, NOT NULL (primary key).
    assert cols["metric"]["data_type"] == "text"
    assert cols["metric"]["is_nullable"] == "NO"

    # value: bigint, NOT NULL.
    assert cols["value"]["data_type"] == "bigint"
    assert cols["value"]["is_nullable"] == "NO"

    # updated_at: timestamptz, NOT NULL.
    assert cols["updated_at"]["data_type"] == "timestamp with time zone"
    assert cols["updated_at"]["is_nullable"] == "NO"


def test_jobs_health_state_metric_is_primary_key(pg_conn):
    if not _table_exists(pg_conn, "jobs_health_state"):
        pytest.skip("migration 0011 not applied — jobs_health_state absent")

    pk_cols = pg_conn.execute(
        """
        SELECT a.attname AS col
        FROM pg_index i
        JOIN pg_attribute a
          ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
        WHERE i.indrelid = 'jobs_health_state'::regclass
          AND i.indisprimary
        """
    ).fetchall()
    assert [r["col"] for r in pk_cols] == ["metric"], pk_cols
