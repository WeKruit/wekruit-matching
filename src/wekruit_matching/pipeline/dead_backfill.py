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

# rank-15: a `dead=TRUE` flag must persist at least this long before
# reconcile_dead_inactive acts on it, so a single transient liveness miss does
# not yank a live job from the matcher (recovery would otherwise wait for the
# 90-day dead-retry). permanent_404 tombstones bypass this (they are confirmed).
# Tunable via env for ops.
import os  # noqa: E402

RECONCILE_DEAD_GRACE_HOURS = int(os.environ.get("RECONCILE_DEAD_GRACE_HOURS", "24"))


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


def reconcile_dead_inactive(conn: psycopg.Connection) -> int:
    """Flip active jobs that are dead / permanent_404 to status='inactive'.

    Durable root-fix for the "dead jobs served to users" defect (2026-05-29).
    Both this module (``firestore_dead_backfill`` mirrors ``dead=true``) and the
    Stage 2b JD path (``permanent_404=true``) set the dead flags WITHOUT flipping
    ``status``, and ``scraper.upsert._filter_dead_tombstoned`` only SKIPS dead
    rows from the upsert INPUT — it never deactivates an already-active dead row.
    So confirmed-dead postings accumulate at ``status='active'`` and (absent the
    ``job_sync`` dead-filter) rode into the live matcher as clickable "matches"
    that 404. Live audit found 1,893 such rows.

    Run this AFTER all dead-marking and BEFORE the Firestore sync so the
    inactive-sync removes them from the live matcher. It only touches ``status``
    (no other column), so the 90-day dead-retry path in ``upsert`` — which keys
    on the ``dead`` flag at any status — still re-activates a genuinely re-listed
    job on schedule.

    Idempotent: re-running flips 0 once the corpus is clean. Caller-agnostic
    commit (commits its own single UPDATE). Returns rows flipped.
    """
    # rank-15 debounce: a `dead=TRUE` flag from a SINGLE transient liveness
    # miss should not immediately inactivate a live row (one flaky FS dead
    # mirror or one 404 blip would yank a real job from the matcher, then the
    # 90-day retry is the only way back). Require the dead flag to have persisted
    # at least RECONCILE_DEAD_GRACE before acting. permanent_404 is a hard
    # tombstone (set only on a confirmed-gone ATS page) so it flips immediately.
    # A NULL dead_confirmed_at is legacy/already-confirmed -> treated as aged.
    result = conn.execute(
        f"""
        UPDATE jobs
        SET status = 'inactive'
        WHERE status = 'active'
          AND (
            COALESCE(permanent_404, FALSE) = TRUE
            OR (
              COALESCE(dead, FALSE) = TRUE
              AND (
                dead_confirmed_at IS NULL
                OR dead_confirmed_at < NOW() - INTERVAL '{RECONCILE_DEAD_GRACE_HOURS} hours'
              )
            )
          )
        """
    )
    conn.commit()
    flipped = result.rowcount
    if flipped:
        logger.info(
            "pa.reconcile_dead_inactive flipped={} dead/404 active->inactive",
            flipped,
        )
    else:
        logger.info("pa.reconcile_dead_inactive flipped=0 (clean)")
    return flipped


# ---------------------------------------------------------------------------
# Firestore <-> Postgres reconciliation gate (fix #5, 2026-05-30)
# ---------------------------------------------------------------------------
#
# Distinct from health_gate's PG-side data-quality FLOORS: this compares the PG
# matchable set against the count actually present in the live Firestore
# matching-jobs collection, catching FS-vs-PG active drift (stale-active docs in
# Firestore that PG no longer considers matchable, or matchable PG rows the sync
# never delivered). It NEVER gates the run; the daily orchestrator surfaces a
# divergence as stage_outcomes['firestore_reconcile']='degraded'.
#
# Graceful skip mirrors firestore_dead_backfill: if google-cloud-firestore is
# not installed or creds are unavailable, return skipped=True (the caller then
# falls back to a PG-vs-sync-reported comparison). Never prints SA contents.


def _firestore_client_for_reconcile(project_id: str | None):
    """Build a read-only Firestore client, or None if unavailable.

    Resolves the project via the explicit ``project_id`` (settings /
    ``FIRESTORE_PROJECT_ID``) falling back to the dead-backfill default, and
    relies on Application Default Credentials (``GOOGLE_APPLICATION_CREDENTIALS``
    / ``firebase_service_account_json``) — NEVER a hardcoded SA path. Any
    import/credential failure returns None so the caller skips gracefully.
    """
    try:
        from google.cloud import firestore  # type: ignore[import-not-found]
    except Exception as e:  # SDK not installed in this env
        logger.warning(
            "firestore_reconcile SKIPPED (google-cloud-firestore not installed: {})",
            e,
        )
        return None
    try:
        return firestore.Client(project=project_id or _FIREBASE_PROJECT_ID)
    except Exception as e:  # DefaultCredentialsError, etc.
        logger.warning("firestore_reconcile SKIPPED (Firestore client init failed: {})", e)
        return None


def _pg_matchable_count(conn) -> int:
    """Count PG rows matching the EXACT Firestore sync-gate predicate
    (job_sync._fetch_active_jobs)."""
    result = conn.execute(
        """
        SELECT count(*) AS n FROM jobs
        WHERE status = 'active'
          AND COALESCE(dead, FALSE) = FALSE
          AND COALESCE(permanent_404, FALSE) = FALSE
          AND embedding IS NOT NULL
          AND embedded_at IS NOT NULL
          AND job_description IS NOT NULL
          AND length(job_description) >= 200
          AND required_skills IS NOT NULL
          AND cardinality(required_skills) > 0
        """
    )
    row = result.fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(next(iter(row.values())))
    return int(row[0])


def _firestore_count(client, collection: str, *, active_only: bool) -> int:
    """Count docs in a Firestore collection.

    Prefers the server-side COUNT aggregation (cheap, no doc reads); falls back
    to streaming doc ids. ``active_only`` filters status=='active' to mirror the
    PG matchable set as closely as the receiver schema allows.
    """
    ref = client.collection(collection)
    query = ref
    if active_only:
        try:
            query = ref.where("status", "==", "active")
        except Exception:  # pragma: no cover - defensive
            query = ref
    try:
        agg = query.count()
        for chunk in agg.get():
            for item in chunk:
                return int(item.value)
    except Exception as e:
        logger.warning("firestore_reconcile count() aggregation failed ({}); streaming", e)
    n = 0
    for _ in query.stream():
        n += 1
    return n


def firestore_reconcile(
    conn,
    *,
    threshold: float = 0.05,
    project_id: str | None = None,
    collection: str | None = None,
    client_factory=None,
) -> dict:
    """Reconcile the PG matchable set against the live Firestore collection.

    Returns a dict: ``{ok, skipped, divergence, pg_matchable, fs_active,
    fs_total, reason}``. ``ok`` is False when
    ``abs(pg - fs_active) / max(pg, 1) > threshold``. ``skipped=True`` (with
    ``ok=True``) when the Firestore client/creds are unavailable, so the gate
    does not fail merely for lack of read access in a given environment. Never
    raises for a Firestore read error — degrades to skipped.

    ``client_factory`` / ``project_id`` / ``collection`` are injectable for
    tests; production resolves them from settings with safe defaults.
    """
    # Resolve project + collection from settings (lazy import to avoid pulling
    # config at module load; matches the rest of this module's local-import
    # style). Fall back to the dead-backfill defaults.
    if project_id is None or collection is None:
        try:
            from wekruit_matching.config import get_settings

            settings = get_settings()
            project_id = project_id or (settings.firestore_project_id or None)
            collection = collection or settings.firebase_sync_collection
        except Exception:  # config unavailable — use module defaults
            collection = collection or _FIREBASE_COLLECTION
    collection = collection or _FIREBASE_COLLECTION

    pg_matchable = _pg_matchable_count(conn)

    factory = client_factory or (lambda: _firestore_client_for_reconcile(project_id))
    client = factory()
    if client is None:
        return {
            "ok": True,
            "skipped": True,
            "divergence": 0.0,
            "pg_matchable": pg_matchable,
            "fs_active": 0,
            "fs_total": 0,
            "reason": "firestore client unavailable",
        }

    try:
        fs_total = _firestore_count(client, collection, active_only=False)
        fs_active = _firestore_count(client, collection, active_only=True)
    except Exception as e:
        logger.warning("firestore_reconcile read failed (skipping): {}", e)
        return {
            "ok": True,
            "skipped": True,
            "divergence": 0.0,
            "pg_matchable": pg_matchable,
            "fs_active": 0,
            "fs_total": 0,
            "reason": f"firestore read error: {e}",
        }

    denom = max(pg_matchable, 1)
    divergence = abs(pg_matchable - fs_active) / denom
    ok = divergence <= threshold
    return {
        "ok": ok,
        "skipped": False,
        "divergence": divergence,
        "pg_matchable": pg_matchable,
        "fs_active": fs_active,
        "fs_total": fs_total,
        "reason": ""
        if ok
        else (
            f"PG matchable={pg_matchable} vs Firestore active={fs_active} "
            f"diverge {divergence:.1%} > {threshold:.0%}"
        ),
    }
