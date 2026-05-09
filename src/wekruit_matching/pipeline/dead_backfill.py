"""Stage 0 — Pull dead-flag set from Firestore into Postgres tombstones.

P7-K (2026-05-09) — Hybrid TTL + tombstone

Runs at the very start of the daily pipeline (before any scraper writes
to ``jobs``) so that:

1. The set of URLs that ``paLivenessSweepDaily`` confirmed dead overnight
   on the wekruit-pa side (Firestore ``matching-jobs`` where ``dead==true``)
   is mirrored into Postgres ``jobs.dead`` / ``dead_confirmed_at``.
2. The subsequent Stage 1+ scrape's UPSERT can short-circuit on already-
   dead URLs (see ``scraper.upsert._filter_dead_tombstoned``).

Behaviour
---------
* Idempotent: ``COALESCE(dead_confirmed_at, NOW())`` so repeat runs don't
  reset the timestamp on already-tombstoned rows.
* Graceful skip: if Firestore creds aren't configured (no
  ``GOOGLE_APPLICATION_CREDENTIALS`` env or ``google-cloud-firestore``
  not installed), logs a warning and returns 0 — the pipeline continues.
  This is intentional so the dependency rollout doesn't break existing
  prod runs the day this ships.
* Page size 500: Firestore single-query limit + matches typical dead set
  size. Larger sets stream across multiple pages.
* Logs ``pa.macmini.dead_backfill {synced: N, total_seen: M}`` so ops
  dashboards can sanity-check.

Integration
-----------
Called from ``pipeline.daily.run_daily_pipeline`` inside the existing
``_stage_timeout("dead_backfill", 5*60)`` pattern from P7-B. Stage
budget: 5 min. Failures are recorded into ``errors`` and the pipeline
moves on (the scraper still runs without skip-dead awareness — slightly
worse but not broken).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterator

import psycopg
from loguru import logger


# Default page size: balances Firestore RPC overhead vs single-query limit
# (Firestore stream() handles paging transparently; this caps in-memory
# accumulation per UPDATE batch).
_BACKFILL_BATCH_SIZE = 500
_FIREBASE_PROJECT_ID = "wekruit-5f89b"
_FIREBASE_COLLECTION = "matching-jobs"


def _iter_dead_doc_ids(project_id: str, collection: str) -> Iterator[tuple[str, datetime | None]]:
    """Stream (job_id, deadAt) pairs for all dead docs in Firestore.

    Wrapped in its own function so tests can monkeypatch without touching
    the SDK at import time.

    Returns an iterator so callers can batch lazily without holding the
    full result set in memory.

    Raises:
        ImportError if google-cloud-firestore is not installed.
        google.auth.exceptions.DefaultCredentialsError if creds missing.
    """
    # Local import: ``google-cloud-firestore`` is an optional dep that may
    # not be available in every environment yet. The caller catches.
    from google.cloud import firestore  # type: ignore[import-not-found]

    client = firestore.Client(project=project_id)
    query = client.collection(collection).where("dead", "==", True)
    for snap in query.stream():
        data = snap.to_dict() or {}
        # Firestore field name observed in prod: ``deadCheckedAt`` (set by
        # ``paLivenessSweepDaily``). Older or other writers may use
        # ``deadAt`` / ``dead_confirmed_at``. Accept all three.
        confirmed = (
            data.get("deadCheckedAt")
            or data.get("deadAt")
            or data.get("dead_confirmed_at")
        )
        # Normalize types: prod stores as ISO-8601 string, the SDK can also
        # surface DatetimeWithNanoseconds. Convert to tz-aware UTC datetime
        # (or None for legacy docs missing the field).
        if isinstance(confirmed, str):
            try:
                # Replace trailing 'Z' so fromisoformat accepts it on 3.10+
                iso = confirmed[:-1] + "+00:00" if confirmed.endswith("Z") else confirmed
                confirmed = datetime.fromisoformat(iso)
            except ValueError:
                confirmed = None
        if confirmed is not None and getattr(confirmed, "tzinfo", None) is None:
            confirmed = confirmed.replace(tzinfo=UTC)
        yield snap.id, confirmed


def firestore_dead_backfill(
    conn: psycopg.Connection,
    *,
    project_id: str = _FIREBASE_PROJECT_ID,
    collection: str = _FIREBASE_COLLECTION,
    batch_size: int = _BACKFILL_BATCH_SIZE,
    iter_factory=None,
) -> dict[str, int]:
    """Mirror Firestore ``dead==true`` docs into Postgres ``jobs.dead``.

    Args:
        conn: open psycopg connection (caller owns commit/rollback).
        project_id: Firebase project ID (defaults to wekruit prod).
        collection: Firestore collection name (defaults to matching-jobs).
        batch_size: how many job_ids to UPDATE per round-trip.
        iter_factory: pluggable iterator (for tests). Defaults to the live
            Firestore stream.

    Returns: ``{"synced": N, "total_seen": M, "skipped": "configured?"}``.
        On graceful skip (no creds / SDK missing), returns
        ``{"synced": 0, "total_seen": 0, "skipped": "no_creds"}``.
    """
    if iter_factory is None:
        iter_factory = lambda: _iter_dead_doc_ids(project_id, collection)

    # Try to obtain the iterator. SDK or creds missing → graceful skip.
    try:
        it = iter_factory()
    except ImportError as e:
        logger.warning(
            "pa.macmini.dead_backfill SKIPPED (google-cloud-firestore not installed: {})",
            e,
        )
        return {"synced": 0, "total_seen": 0, "skipped": "no_sdk"}
    except Exception as e:  # DefaultCredentialsError, etc.
        logger.warning(
            "pa.macmini.dead_backfill SKIPPED (Firestore client init failed: {})",
            e,
        )
        return {"synced": 0, "total_seen": 0, "skipped": "no_creds"}

    total_seen = 0
    synced = 0
    pending: list[dict] = []

    def _flush() -> int:
        if not pending:
            return 0
        ids = [p["id"] for p in pending]
        # Pair the timestamps via UNNEST so each row gets its actual
        # confirmed-at, not a single value across the batch. NULL is
        # preserved for legacy docs without a timestamp.
        timestamps = [p["confirmed_at"] for p in pending]
        result = conn.execute(
            """
            UPDATE jobs AS j
            SET dead = TRUE,
                dead_confirmed_at = COALESCE(
                    j.dead_confirmed_at,
                    src.confirmed_at,
                    NOW()
                )
            FROM (
                SELECT UNNEST(%(ids)s::text[]) AS job_id,
                       UNNEST(%(ts)s::timestamptz[]) AS confirmed_at
            ) AS src
            WHERE j.job_id = src.job_id
            """,
            {"ids": ids, "ts": timestamps},
        )
        conn.commit()
        return result.rowcount

    try:
        for job_id, confirmed_at in it:
            total_seen += 1
            pending.append({"id": job_id, "confirmed_at": confirmed_at})
            if len(pending) >= batch_size:
                synced += _flush()
                pending.clear()
        # Final flush
        synced += _flush()
    except Exception as e:
        logger.warning(
            "pa.macmini.dead_backfill PARTIAL (stream raised after seen={} synced={}): {}",
            total_seen, synced, e,
        )
        # Best-effort flush on whatever we have so far
        try:
            synced += _flush()
        except Exception:
            pass

    logger.info(
        "pa.macmini.dead_backfill synced={} total_seen={}",
        synced, total_seen,
    )
    return {"synced": synced, "total_seen": total_seen, "skipped": ""}
