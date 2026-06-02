"""Startup schema-current guard (reliability audit 2026-06-01, CID-05).

Why this exists
===============
An entrypoint that forgets to run ``alembic upgrade head`` before doing work
runs against a DB whose schema is OLDER than the code expects — the silent
"schema-vs-code skew" class. The producer fix (daily-update.sh runs
``alembic upgrade head`` + a head-assert before launching the pipeline) stops
the common path; this guard is the belt-and-suspenders that catches ANY
entrypoint which skips that step, by asserting at startup that the DB's current
alembic revision matches the migration tree's head(s).

``ensure_schema_current`` compares:
  * the head revision(s) of the on-disk migration tree (alembic ScriptDirectory)
  * the ``version_num`` recorded in the DB's ``alembic_version`` table

and raises ``RuntimeError`` on a real mismatch so the caller can fail fast.

Resilience contract
-------------------
This runs FIRST in pipeline.daily, where the connection may be a test double or
a DB whose ``alembic_version`` is not yet readable. The guard fails CLOSED only
on a *genuine* skew (the DB reports a real revision string that is not a head);
it fails OPEN (logs a warning and returns without raising) when it cannot
determine the DB revision at all (no table, unreadable, or a non-string value
from a mock). Rationale: in production daily-update.sh applies migrations and
head-asserts immediately before the pipeline, so an unreadable revision here is
a transient/test condition, not the skew this guard exists to catch — and we
must never wedge the night on an unverifiable read.

Import-light: alembic ``Config``/``ScriptDirectory`` are imported lazily inside
the function so importing this module is cheap and side-effect-free.
"""

from __future__ import annotations

import os
from pathlib import Path

from loguru import logger

# Repo-root-relative alembic config. schema_guard lives at
# src/wekruit_matching/db/schema_guard.py -> repo root is parents[3].
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"
_ALEMBIC_DIR = _REPO_ROOT / "alembic"


def _script_heads() -> set[str]:
    """Return the set of head revision id(s) from the on-disk migration tree.

    Uses an explicit ``script_location`` so this works regardless of the
    process CWD (the daily run cd's to the repo root, but other callers may
    not). Imports alembic lazily to keep module import cheap.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_ALEMBIC_INI) if _ALEMBIC_INI.exists() else None)
    # script_location may be unset/relative in alembic.ini; pin it absolutely.
    cfg.set_main_option("script_location", str(_ALEMBIC_DIR))
    script = ScriptDirectory.from_config(cfg)
    return set(script.get_heads())


def _db_current_revision(conn) -> str | None:
    """Read the current revision from the DB's ``alembic_version`` table.

    Returns the revision string, or ``None`` when it cannot be determined
    (table absent, empty, unreadable, or a non-string value e.g. from a test
    double). Never raises.
    """
    try:
        row = conn.execute(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ).fetchone()
    except Exception as e:  # noqa: BLE001 - table may not exist / mock / transient
        logger.warning("[schema_guard] could not read alembic_version: {}", e)
        return None
    if row is None:
        return None
    # dict_row (production) -> mapping; tuple rows -> index 0.
    value = None
    if isinstance(row, dict):
        value = row.get("version_num")
    else:
        try:
            value = row[0]
        except Exception:  # noqa: BLE001
            value = None
    # Only a real revision string is a usable signal. A non-string (e.g. a
    # MagicMock from a unit-test connection) means "cannot determine".
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def ensure_schema_current(conn_or_url=None) -> None:
    """Assert the DB schema is at the migration tree's head; raise on skew.

    Args:
        conn_or_url: optionally an open psycopg connection (preferred — reuses
            the caller's pool connection) OR a libpq/SQLAlchemy URL string. When
            ``None``, a short-lived connection is opened via
            ``db.connection.get_connection``.

    Raises:
        RuntimeError: when the DB reports a real current revision that is NOT a
            head of the on-disk migration tree (genuine schema-vs-code skew).

    Fails OPEN (logs + returns) when the DB revision cannot be determined, so an
    unverifiable read never wedges the run (see module docstring).
    """
    heads = _script_heads()
    if not heads:
        logger.warning(
            "[schema_guard] no migration heads found on disk; skipping check"
        )
        return

    # Resolve a connection.
    if conn_or_url is None:
        from wekruit_matching.db.connection import get_connection

        with get_connection() as conn:
            current = _db_current_revision(conn)
    elif isinstance(conn_or_url, str):
        import psycopg
        from psycopg.rows import dict_row

        url = conn_or_url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(url, row_factory=dict_row) as conn:
            current = _db_current_revision(conn)
    else:
        # Assume an already-open connection object.
        current = _db_current_revision(conn_or_url)

    if current is None:
        # Could not determine the DB revision — fail open (do not wedge). In
        # production daily-update.sh has already applied + head-asserted.
        logger.warning(
            "[schema_guard] DB current revision undeterminable; skipping "
            "skew check (heads={})",
            sorted(heads),
        )
        return

    if current not in heads:
        raise RuntimeError(
            "schema-vs-code skew: DB alembic revision "
            f"{current!r} is not a migration head {sorted(heads)!r}. "
            "Run 'alembic upgrade head' before starting this entrypoint "
            "(the code expects a newer schema than the database has)."
        )

    logger.info(
        "[schema_guard] schema current: DB revision {} == head", current
    )


def _cli_url() -> str | None:
    return (os.environ.get("DATABASE_URL") or "").strip() or None


def main() -> int:
    """CLI: exit 0 if schema is current (or undeterminable), 1 on genuine skew."""
    try:
        ensure_schema_current(_cli_url())
    except RuntimeError as e:
        logger.error("[schema_guard] {}", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
