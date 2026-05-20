"""Batch sync embedded jobs to Firebase over HTTP."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from itertools import islice
from typing import Any

import httpx
from loguru import logger

from wekruit_matching.config import get_settings
from wekruit_matching.db.connection import get_connection
from wekruit_matching.scraper.id_utils import compute_canonical_signature


def _serialize_embedding(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            inner = stripped[1:-1].strip()
            if not inner:
                return []
            return [float(item) for item in inner.split(",")]
    if hasattr(value, "tolist"):
        return _serialize_embedding(value.tolist())
    return value


def _serialize_job(row: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
        elif key == "embedding":
            payload[key] = _serialize_embedding(value)
        elif isinstance(value, tuple):
            payload[key] = list(value)
        else:
            payload[key] = value
    # Track E (matching-quality launch blocker, 2026-05-20): emit the
    # cross-source canonical signature so wekruit-pa can read it from
    # Firestore for the `pa-job-canonical-signature/{sig}` dedup index.
    # company_name + role_title are required scraper fields — if either is
    # missing, the row would have failed Track D's sync gate upstream, so
    # we skip the signature rather than emit a wrong/partial one.
    #
    # v2 (2026-05-20): signature now includes location_raw to disambiguate
    # multi-posting same role at same company (e.g. Google SWE SF vs NYC).
    # location_raw may be empty — normalize_location collapses empty to
    # `__no_loc__`, preserving the v1 behaviour for rows that lack location.
    co = payload.get("company_name")
    role = payload.get("role_title")
    loc = payload.get("location_raw")
    if isinstance(co, str) and isinstance(role, str) and co.strip() and role.strip():
        loc_arg = loc if isinstance(loc, str) else None
        payload["canonical_signature"] = compute_canonical_signature(co, role, loc_arg)
    return payload


def _batched(items: list[dict[str, Any]], batch_size: int) -> Iterable[list[dict[str, Any]]]:
    iterator = iter(items)
    while batch := list(islice(iterator, batch_size)):
        yield batch


def _should_split_failed_batch(status_code: int, response_text: str) -> bool:
    lowered = response_text.lower()
    return status_code in {503, 504} or any(
        marker in lowered
        for marker in (
            "transaction too big",
            "deadline exceeded",
            "resource exhausted",
            "service unavailable",
        )
    )


def _post_jobs_batch(
    *,
    url: str,
    headers: dict[str, str],
    collection: str,
    mode: str,
    jobs: list[dict[str, Any]],
    timeout: float,
) -> int:
    try:
        response = httpx.post(
            url,
            headers=headers,
            json={
                "collection": collection,
                "mode": mode,
                "jobs": jobs,
            },
            timeout=timeout,
        )
    except httpx.TimeoutException:
        if len(jobs) <= 1:
            raise
        midpoint = len(jobs) // 2
        logger.warning(
            "Firebase sync batch timed out at {} jobs; retrying as {} + {}",
            len(jobs),
            midpoint,
            len(jobs) - midpoint,
        )
        return _post_jobs_batch(
            url=url,
            headers=headers,
            collection=collection,
            mode=mode,
            jobs=jobs[:midpoint],
            timeout=timeout,
        ) + _post_jobs_batch(
            url=url,
            headers=headers,
            collection=collection,
            mode=mode,
            jobs=jobs[midpoint:],
            timeout=timeout,
        )

    response_ok = getattr(response, "is_success", None)
    if response_ok is None:
        try:
            response.raise_for_status()
            return 1
        except httpx.HTTPStatusError as exc:
            response = exc.response
            response_ok = False

    if response_ok:
        return 1

    if len(jobs) > 1 and _should_split_failed_batch(response.status_code, response.text):
        midpoint = len(jobs) // 2
        logger.warning(
            "Firebase sync batch failed at {} jobs with status {}: {}. Retrying as {} + {}",
            len(jobs),
            response.status_code,
            response.text,
            midpoint,
            len(jobs) - midpoint,
        )
        return _post_jobs_batch(
            url=url,
            headers=headers,
            collection=collection,
            mode=mode,
            jobs=jobs[:midpoint],
            timeout=timeout,
        ) + _post_jobs_batch(
            url=url,
            headers=headers,
            collection=collection,
            mode=mode,
            jobs=jobs[midpoint:],
            timeout=timeout,
        )

    response.raise_for_status()
    return 1


def _fetch_active_jobs(
    conn,
    *,
    since: datetime | None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    base_sql = """
        SELECT
            job_id,
            source_repo,
            company_name,
            role_title,
            primary_url,
            ats_apply_url,
            location_raw,
            date_posted_raw,
            status,
            content_hash,
            job_description,
            core_responsibilities,
            salary_range,
            seniority_level,
            role_function,
            sources,
            benefits,
            qualifications,
            industry,
            company_size,
            required_skills,
            sponsorship,
            embedding,
            embedding_model,
            jd_fetch_source,
            first_seen_at,
            last_seen_at,
            enriched_at,
            embedded_at
        FROM jobs
        WHERE status = 'active'
          AND embedding IS NOT NULL
          AND embedded_at IS NOT NULL
          -- Matching-quality launch blocker (Track D, 2026-05-20):
          -- belt-and-suspenders with embedding/worker.py's gate. A job
          -- without a JD body or skills should NEVER land in Firestore
          -- active even if a stale embedding exists from a prior run that
          -- pre-dated the worker gate. This pins the contract at the sync
          -- boundary so an embedding row left behind by older code can't
          -- ride into the matching pool.
          AND job_description IS NOT NULL
          AND length(job_description) >= 200
          AND required_skills IS NOT NULL
          AND cardinality(required_skills) > 0
    """
    params: dict[str, Any] = {}
    if since is None:
        sql = base_sql
    else:
        sql = base_sql + """
          AND embedded_at >= %(since)s
    """
        params["since"] = since

    sql += "\n        ORDER BY embedded_at ASC, job_id ASC"
    if limit is not None:
        sql += "\n        LIMIT %(limit)s\n        OFFSET %(offset)s"
        params["limit"] = limit
        params["offset"] = offset

    return conn.execute(sql, params or None).fetchall()


def _fetch_inactive_jobs(conn) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT
            job_id,
            source_repo,
            company_name,
            role_title,
            primary_url,
            location_raw,
            date_posted_raw,
            status,
            content_hash,
            job_description,
            core_responsibilities,
            salary_range,
            seniority_level,
            role_function,
            sources,
            benefits,
            qualifications,
            industry,
            company_size,
            required_skills,
            sponsorship,
            embedding,
            embedding_model,
            first_seen_at,
            last_seen_at,
            enriched_at,
            embedded_at
        FROM jobs
        WHERE status = 'inactive'
        ORDER BY last_seen_at DESC, job_id ASC
        """
    ).fetchall()


def sync_jobs_to_firebase(
    *,
    since: datetime | None = None,
    full_sync: bool = False,
    active_limit: int | None = None,
    active_offset: int = 0,
    include_inactive: bool = True,
) -> dict[str, int]:
    """Sync job docs to Firebase in HTTP batches.

    Incremental mode syncs active jobs embedded since ``since`` plus all inactive jobs.
    Full mode syncs all active embedded jobs plus all inactive jobs.
    """
    settings = get_settings()
    if not settings.firebase_sync_url:
        raise RuntimeError("FIREBASE_SYNC_URL is not configured")
    if not settings.firebase_sync_api_key:
        raise RuntimeError("FIREBASE_SYNC_API_KEY is not configured")
    if not full_sync and since is None:
        raise ValueError("Incremental sync requires a since timestamp")
    if active_limit is not None and active_limit <= 0:
        raise ValueError("active_limit must be positive")
    if active_offset < 0:
        raise ValueError("active_offset must be non-negative")

    mode = "full" if full_sync else "incremental"

    with get_connection() as conn:
        active_rows = _fetch_active_jobs(
            conn,
            since=None if full_sync else since,
            limit=active_limit,
            offset=active_offset,
        )
        inactive_rows = _fetch_inactive_jobs(conn) if include_inactive else []

    jobs = [_serialize_job(row) for row in [*active_rows, *inactive_rows]]
    batches = list(_batched(jobs, settings.firebase_sync_batch_size))
    headers = {"X-API-Key": settings.firebase_sync_api_key}
    sent_batches = 0

    for index, batch in enumerate(batches, start=1):
        sent_batches += _post_jobs_batch(
            url=settings.firebase_sync_url,
            headers=headers,
            collection=settings.firebase_sync_collection,
            mode=mode,
            jobs=batch,
            timeout=settings.firebase_sync_timeout_seconds,
        )
        logger.info(
            "Synced Firebase batch {}/{} ({} jobs)",
            index,
            len(batches),
            len(batch),
        )

    stats = {
        "active_jobs": len(active_rows),
        "inactive_jobs": len(inactive_rows),
        "synced": len(jobs),
        "batches": sent_batches,
    }
    logger.info("Firebase sync complete: {}", stats)
    return stats
