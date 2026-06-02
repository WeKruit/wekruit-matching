"""Shared pytest fixtures — the DB-backed test contract.

This file is the single source of truth for how integration tests reach
Postgres. It deliberately enforces a *prod-safety guard*: an integration test
will only ever touch a database when BOTH of these hold:

  1. ``WEKRUIT_TEST_DB=1`` is exported, and
  2. the DATABASE_URL's database name ends with ``_test``.

The second clause is the important one — there is a live PROD ``DATABASE_URL``
in ``.env`` on developer machines, and a fixture that connected to whatever
``DATABASE_URL`` happens to be set (the pattern the older ad-hoc DB tests use)
could write to production. Requiring a ``*_test`` database name makes that
mistake structurally impossible: point the fixtures at prod and they *skip*.

Two fixtures are provided:

  * ``pg_url``  (session-scoped) — the raw DATABASE_URL string, or ``pytest.skip``.
  * ``pg_conn`` (function-scoped) — a psycopg3 connection that is ROLLED BACK at
    teardown, so an integration test can never persist a row.

Both skip gracefully if ``psycopg``/``pgvector`` are not importable, so the
non-DB unit suite still runs on a machine without those wheels.
"""
from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest

# Message shown when the prod-safety guard refuses to hand out a DB connection.
_SKIP_MSG = "set WEKRUIT_TEST_DB=1 and a *_test DB to run integration tests"


def _db_name_from_url(url: str) -> str:
    """Extract the database name from a SQLAlchemy/libpq URL, robustly.

    Handles the ``postgresql+psycopg://`` driver prefix and a trailing query
    string (e.g. ``...?sslmode=require``). Returns "" if no DB name is present.
    """
    if not url:
        return ""
    # urlparse copes with the ``postgresql+psycopg`` scheme fine; the dbname is
    # the path with the leading slash stripped, minus any ``?query`` suffix.
    path = urlparse(url).path.lstrip("/")
    # Defensive: strip a query string if it somehow survived into the path.
    return path.split("?", 1)[0]


@pytest.fixture(scope="session")
def pg_url() -> str:
    """Session-scoped DATABASE_URL for integration tests, with a prod-safety guard.

    Skips (rather than fails) unless ``WEKRUIT_TEST_DB=1`` AND the database name
    ends with ``_test``. This is what keeps integration tests from ever touching
    the production database that lives in ``.env``.
    """
    url = os.environ.get("DATABASE_URL", "")
    if os.environ.get("WEKRUIT_TEST_DB") != "1":
        pytest.skip(_SKIP_MSG)
    db_name = _db_name_from_url(url)
    if not db_name.endswith("_test"):
        pytest.skip(_SKIP_MSG)
    return url


@pytest.fixture(scope="function")
def pg_conn(pg_url: str):
    """Function-scoped psycopg3 connection that is ROLLED BACK at teardown.

    Integration tests use this to exercise schema/constraint behaviour without
    persisting anything: every test gets a clean connection and the transaction
    is rolled back when the test finishes (even on failure).

    Skips gracefully if ``psycopg`` (or ``pgvector``) cannot be imported, so the
    pure-Python unit suite is unaffected on machines without those wheels.
    """
    try:
        import psycopg
    except Exception as e:  # noqa: BLE001 — any import failure means "no DB layer"
        pytest.skip(f"psycopg not importable — skipping DB tests: {e}")

    # Convert the SQLAlchemy driver URL to a plain libpq conninfo string.
    conninfo = pg_url.replace("postgresql+psycopg://", "postgresql://", 1)
    try:
        conn = psycopg.connect(conninfo)
    except Exception as e:  # noqa: BLE001 — DB unreachable -> skip, don't fail
        pytest.skip(f"cannot connect to test DB: {e}")

    # Register the pgvector type adapters when the extension lib is present, so
    # tests can bind/read ``vector`` columns. Absence is non-fatal.
    try:
        from pgvector.psycopg import register_vector

        register_vector(conn)
    except Exception:  # noqa: BLE001 — pgvector optional for non-vector tests
        pass

    try:
        yield conn
    finally:
        # Never persist: roll back whatever the test did, then close.
        try:
            conn.rollback()
        finally:
            conn.close()
