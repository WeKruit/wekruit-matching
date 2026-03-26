"""psycopg3 connection pool for the wekruit-matching engine.

Uses psycopg.ConnectionPool (synchronous) — suitable for the batch pipeline scripts.
Async pool (psycopg.AsyncConnectionPool) can be added in Phase 6 if needed.

IMPORTANT: Connection string format must be postgresql+psycopg:// for SQLAlchemy,
but psycopg.ConnectionPool uses the native libpq format:
  postgresql://user:pass@host:port/dbname
Convert at pool creation time.
"""
from contextlib import contextmanager
from functools import lru_cache
from typing import Generator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from wekruit_matching.config import get_settings


def _sqlalchemy_url_to_libpq(url: str) -> str:
    """Convert SQLAlchemy-style URL to libpq format for psycopg.ConnectionPool.

    SQLAlchemy: postgresql+psycopg://user:pass@host:port/dbname
    libpq:      postgresql://user:pass@host:port/dbname
    """
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache(maxsize=1)
def get_pool() -> ConnectionPool:
    """Return a cached psycopg3 ConnectionPool.

    Pool is created once and reused across the process lifetime.
    min_size=1 keeps one connection warm; max_size=5 is sufficient for batch scripts.
    """
    settings = get_settings()
    conninfo = _sqlalchemy_url_to_libpq(settings.database_url)
    pool = ConnectionPool(
        conninfo=conninfo,
        min_size=5,
        max_size=20,
        timeout=5.0,
        max_idle=300.0,
        max_lifetime=1800.0,
        kwargs={"row_factory": dict_row},
    )
    return pool


@contextmanager
def get_connection() -> Generator[psycopg.Connection, None, None]:
    """Context manager that yields a psycopg3 connection from the pool.

    Usage:
        with get_connection() as conn:
            conn.execute("SELECT 1")

    The connection is returned to the pool on context exit (commit/rollback handled by caller).
    """
    pool = get_pool()
    with pool.connection() as conn:
        yield conn
