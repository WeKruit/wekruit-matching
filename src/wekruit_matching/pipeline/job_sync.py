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
        # Tag-ownership boundary (2026-05-29): role_function and
        # seniority_level in Postgres are filled by the macmini *regex*
        # heuristics (infer_role_function / infer_seniority in
        # title_inference.py). The canonical, LLM-derived values are owned by
        # the wekruit-pa `paMatchingJobsAutoEnrich` Firestore trigger
        # (@pa/job-tag-enricher, an LLM router) which writes the matcher's
        # `roleFunction` (D1 hard filter) and `seniorityLevel`.
        #
        # Verified on live data: the sync receiver (matching-api
        # buildMatchingJobRecord) does NOT even map role_function (pa is the
        # sole writer), and DOES map seniority_level — so our regex value
        # raced pa's LLM value and left Firestore seniorityLevel split across
        # two vocabularies (entry vs entry_level), silently dropping matches
        # under the exact-match query. The receiver upserts with merge:true,
        # so OMITTING these keys preserves pa's canonical values instead of
        # clobbering them. We therefore stop emitting both from the sync.
        if key in ("role_function", "seniority_level"):
            continue
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
        elif key == "embedding":
            payload[key] = _serialize_embedding(value)
        elif key == "sponsorship" and value is None:
            # 2026-06-07: serve an explicit "unknown" instead of NULL so the
            # downstream matcher/UI shows a clear value rather than a blank for
            # jobs whose JD doesn't state visa sponsorship. A boolean consumer
            # treats "unknown" exactly like NULL did (neither == true nor false,
            # so excluded from both "sponsors" and "no-sponsor" filters) — no
            # behaviour change, just clarity. Postgres keeps NULL, so the
            # health-gate's sponsorship-coverage signal stays honest.
            payload[key] = "unknown"
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
    """Return True if a failed batch should be retried as smaller halves.

    Includes client 400/413/422: a batch-level rejection is almost always ONE
    malformed doc among many (oversized field vs Firestore's 1MB limit, a value
    the receiver schema rejects, a missing required key). Bisecting isolates that
    doc to a single-job batch, where the terminal handler in ``_post_jobs_batch``
    logs and SKIPS it instead of crashing the whole sync stage. Without this, one
    bad doc in a 351-batch daily run aborts every remaining batch (observed
    2026-05-30: only 2/351 batches synced before a batch-3 400 killed Stage 4).
    """
    lowered = response_text.lower()
    return status_code in {400, 413, 422, 503, 504} or any(
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
) -> tuple[int, list[str]]:
    """POST one batch. Returns ``(delivered_count, skipped_job_ids)``.

    ``skipped_job_ids`` are docs the receiver terminally rejected (client 4xx
    narrowed to a single doc) — they were NOT delivered. The caller MUST exclude
    them from the synced-hash ledger and the watermark advance, else a rejected
    doc is silently recorded as synced and never re-sent (the 2026-06-01
    seam_embed_sync-1 permanent-drop bug). Recursive bisection unions the
    skipped ids of both halves.
    """
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
        left_n, left_skip = _post_jobs_batch(
            url=url,
            headers=headers,
            collection=collection,
            mode=mode,
            jobs=jobs[:midpoint],
            timeout=timeout,
        )
        right_n, right_skip = _post_jobs_batch(
            url=url,
            headers=headers,
            collection=collection,
            mode=mode,
            jobs=jobs[midpoint:],
            timeout=timeout,
        )
        return left_n + right_n, left_skip + right_skip

    response_ok = getattr(response, "is_success", None)
    if response_ok is None:
        try:
            response.raise_for_status()
            return 1, []
        except httpx.HTTPStatusError as exc:
            response = exc.response
            response_ok = False

    if response_ok:
        return len(jobs), []

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
        left_n, left_skip = _post_jobs_batch(
            url=url,
            headers=headers,
            collection=collection,
            mode=mode,
            jobs=jobs[:midpoint],
            timeout=timeout,
        )
        right_n, right_skip = _post_jobs_batch(
            url=url,
            headers=headers,
            collection=collection,
            mode=mode,
            jobs=jobs[midpoint:],
            timeout=timeout,
        )
        return left_n + right_n, left_skip + right_skip

    # Terminal case: a single doc the receiver rejects with a client 4xx
    # (400 malformed / 413 too large / 422 invalid field). Bisecting above has
    # narrowed the failure to this one job. Do NOT raise — that would crash the
    # entire sync stage and drop every other doc (and every later batch). Log
    # the offending job_id + reason and report it as SKIPPED (not delivered) so
    # the caller keeps it OUT of the synced-hash ledger and the watermark — a
    # skipped doc must remain re-selectable on the next run, not be silently
    # marked synced. Auth (401/403) and unexpected statuses still raise loud.
    if len(jobs) == 1 and response.status_code in {400, 413, 422}:
        bad_id = jobs[0].get("job_id") if jobs else None
        logger.error(
            "Firebase sync: SKIPPING job {} — receiver rejected with {}: {}",
            bad_id,
            response.status_code,
            response.text[:300],
        )
        return 0, ([bad_id] if bad_id is not None else [])

    response.raise_for_status()
    return 1, []


def _fetch_active_jobs(
    conn,
    *,
    since: datetime | None,
    limit: int | None = None,
    offset: int = 0,
    include_changed_hash: bool = False,
) -> list[dict[str, Any]]:
    # Fix #4: LEFT JOIN the content_hash ledger so the incremental window can
    # also catch rows whose ONLY change is content_hash (e.g. a Stage 2.5 ATS
    # url resolve that did not advance embedded_at). The base matchable
    # predicate is unchanged; the join adds no rows on its own. The jobs table
    # is aliased ``j`` so the join column references are unambiguous.
    base_sql = f"""
        SELECT
            j.job_id,
            j.source_repo,
            j.company_name,
            j.role_title,
            j.primary_url,
            j.ats_apply_url,
            j.location_raw,
            j.date_posted_raw,
            j.status,
            j.content_hash,
            j.job_description,
            j.core_responsibilities,
            j.salary_range,
            j.seniority_level,
            j.role_function,
            j.sources,
            j.benefits,
            j.qualifications,
            j.industry,
            j.company_size,
            j.required_skills,
            j.sponsorship,
            j.embedding,
            j.embedding_model,
            j.jd_fetch_source,
            j.first_seen_at,
            j.last_seen_at,
            j.enriched_at,
            j.embedded_at
        FROM jobs j
        LEFT JOIN {_SYNCED_HASHES_TABLE} sh ON sh.job_id = j.job_id
        WHERE j.status = 'active'
          -- Reliability (2026-05-29): a liveness sweep sets dead=true /
          -- permanent_404=true WITHOUT flipping status, so a confirmed-dead
          -- posting stayed status='active' and rode into Firestore — the user
          -- clicked a match and the job was gone (1,792 such docs found in the
          -- live matchable set on 2026-05-29). Exclude dead/404 at the sync
          -- boundary, belt-and-suspenders with the Track-D JD/skills gate below.
          AND COALESCE(j.dead, FALSE) = FALSE
          AND COALESCE(j.permanent_404, FALSE) = FALSE
          AND j.embedding IS NOT NULL
          AND j.embedded_at IS NOT NULL
          -- Matching-quality launch blocker (Track D, 2026-05-20):
          -- belt-and-suspenders with embedding/worker.py's gate. A job
          -- without a JD body or skills should NEVER land in Firestore
          -- active even if a stale embedding exists from a prior run that
          -- pre-dated the worker gate. This pins the contract at the sync
          -- boundary so an embedding row left behind by older code can't
          -- ride into the matching pool.
          AND j.job_description IS NOT NULL
          AND length(j.job_description) >= 200
          AND j.required_skills IS NOT NULL
          AND cardinality(j.required_skills) > 0
    """
    params: dict[str, Any] = {}
    if since is None:
        sql = base_sql
    elif include_changed_hash:
        # Fix #4: embedded_at window OR a content_hash that differs from the
        # last value we successfully synced for this job (NULL ledger row =>
        # never-synced-with-this-hash => DISTINCT FROM is TRUE => included).
        sql = base_sql + """
          AND (j.embedded_at >= %(since)s
               OR sh.content_hash IS DISTINCT FROM j.content_hash)
    """
        params["since"] = since
    else:
        sql = base_sql + """
          AND j.embedded_at >= %(since)s
    """
        params["since"] = since

    sql += "\n        ORDER BY j.embedded_at ASC, j.job_id ASC"
    if limit is not None:
        sql += "\n        LIMIT %(limit)s\n        OFFSET %(offset)s"
        params["limit"] = limit
        params["offset"] = offset

    return conn.execute(sql, params or None).fetchall()


def _ensure_synced_hashes_table(conn) -> None:
    """Idempotent DDL for the per-job content_hash ledger (fix #4)."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_SYNCED_HASHES_TABLE} (
            job_id       text PRIMARY KEY,
            content_hash text,
            synced_at    timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def _record_synced_hashes(conn, rows: list[dict[str, Any]]) -> None:
    """Record the content_hash shipped for each pushed job (fix #4).

    Called only after a fully-successful active push so a content_hash-only
    change is re-selected exactly once (until confirmed synced here). Accepts
    dict rows only; tuple rows are skipped (no key access).
    """
    pairs = [
        (r.get("job_id"), r.get("content_hash"))
        for r in rows
        if isinstance(r, dict) and r.get("job_id") is not None
    ]
    if not pairs:
        return
    for job_id, content_hash in pairs:
        conn.execute(
            f"""
            INSERT INTO {_SYNCED_HASHES_TABLE} (job_id, content_hash, synced_at)
            VALUES (%(j)s, %(h)s, now())
            ON CONFLICT (job_id) DO UPDATE
                SET content_hash = EXCLUDED.content_hash,
                    synced_at    = now()
            """,
            {"j": job_id, "h": content_hash},
        )
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


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


# --- Durable incremental sync watermark -------------------------------------
#
# Reliability fix (2026-05-29): incremental sync was driven purely by the
# caller's ``since`` (daily.py passes ``run_started_at`` — the wall clock at
# pipeline start). If a run embedded jobs and then the Firestore push partially
# failed (timeout / 503 / crash mid-batch), those rows kept their past
# ``embedded_at`` while the NEXT run advanced ``since`` to a later wall clock —
# so the un-synced jobs were SILENTLY skipped forever and the live matcher
# never saw them. Symptom: "matching unreliable, a new issue every day".
#
# We persist a durable high-watermark = the max ``embedded_at`` that was *fully*
# synced. Each incremental run resumes from ``min(caller_since, watermark)``
# (re-covering any window a prior run failed on) and only advances the watermark
# AFTER every batch succeeds. The Firestore receiver upserts by job_id, so
# re-sending the overlap is idempotent and safe.
#
# The state table is created idempotently (CREATE TABLE IF NOT EXISTS) — a
# non-destructive DDL that requires no separate migration step.
_SYNC_STATE_KEY = "firebase_active_embedded_at"

# Fix #4 (2026-05-30) — content_hash-only sync gap.
# The incremental window above is keyed solely on ``embedded_at``. Stage 2.5
# (ATS resolve) bumps a row's ``content_hash`` (and ats_apply_url) WITHOUT
# touching ``embedded_at``; if the row is already embedded, Stage 3 embed skips
# it, so ``embedded_at`` stays in the past and the embedded_at watermark/since
# window MISSES the change — the resolved ats_apply_url never reaches Firestore
# (even though the CF receiver would re-upsert on a content_hash change). We
# close this with a per-job content_hash ledger: after a fully-successful active
# push we record the content_hash we shipped, and the incremental SELECT also
# picks up any matchable row whose current content_hash differs from the last
# one we recorded. This re-sends a content_hash-only change exactly once (until
# confirmed synced), never floods the corpus, and needs no jobs-table migration.
_SYNCED_HASHES_TABLE = "pipeline_synced_hashes"


def _ensure_sync_state_table(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_sync_state (
            key        text PRIMARY KEY,
            watermark  timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def _read_sync_watermark(conn, key: str = _SYNC_STATE_KEY) -> datetime | None:
    rows = conn.execute(
        "SELECT watermark FROM pipeline_sync_state WHERE key = %(key)s",
        {"key": key},
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    # Support both dict_row (production) and tuple rows (defensive).
    if isinstance(row, dict):
        return row.get("watermark")
    return row[0]


def _advance_sync_watermark(conn, watermark: datetime, key: str = _SYNC_STATE_KEY) -> None:
    conn.execute(
        """
        INSERT INTO pipeline_sync_state (key, watermark, updated_at)
        VALUES (%(key)s, %(watermark)s, now())
        ON CONFLICT (key) DO UPDATE
            SET watermark  = EXCLUDED.watermark,
                updated_at = now()
            WHERE pipeline_sync_state.watermark IS NULL
               OR pipeline_sync_state.watermark < EXCLUDED.watermark
        """,
        {"key": key, "watermark": watermark},
    )
    commit = getattr(conn, "commit", None)
    if callable(commit):
        commit()


def _max_embedded_at(rows: list[dict[str, Any]]) -> datetime | None:
    values = [
        r["embedded_at"]
        for r in rows
        if isinstance(r, dict) and isinstance(r.get("embedded_at"), datetime)
    ]
    return max(values) if values else None


def sync_jobs_to_firebase(
    *,
    since: datetime | None = None,
    full_sync: bool = False,
    active_limit: int | None = None,
    active_offset: int = 0,
    include_inactive: bool = True,
    use_watermark: bool = True,
) -> dict[str, int]:
    """Sync job docs to Firebase in HTTP batches.

    Incremental mode syncs active jobs embedded since ``since`` plus all inactive jobs.
    Full mode syncs all active embedded jobs plus all inactive jobs.

    In incremental mode the effective lower bound is
    ``min(since, durable_watermark)`` so jobs that a prior run embedded but
    failed to push (partial sync failure) are re-covered on the next run. The
    durable watermark only advances after every batch succeeds; on any push
    failure it is left untouched so the window is retried. Set
    ``use_watermark=False`` to fall back to the legacy ``since``-only behaviour
    (used by ``active_limit``/``active_offset`` staged backfills, which manage
    their own windows).
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

    # Staged backfills (limit/offset) page through an explicit window and must
    # not be perturbed by the shared watermark.
    is_staged = active_limit is not None or active_offset > 0
    watermark_active = use_watermark and not full_sync and not is_staged

    effective_since = since
    with get_connection() as conn:
        if watermark_active:
            _ensure_sync_state_table(conn)
            stored = _read_sync_watermark(conn)
            if stored is not None and (effective_since is None or stored < effective_since):
                logger.info(
                    "Incremental sync resuming from durable watermark {} "
                    "(caller since={})",
                    stored.isoformat(),
                    effective_since.isoformat() if effective_since else None,
                )
                effective_since = stored

        # Fix #4: in a normal (non-staged) incremental run, also pull rows whose
        # ONLY change is content_hash (e.g. a resolved ATS url that did not
        # advance embedded_at). full_sync already selects everything; staged
        # backfills page a strict embedded_at window and must not widen it.
        include_changed_hash = (
            not full_sync and not is_staged and effective_since is not None
        )
        if include_changed_hash:
            _ensure_synced_hashes_table(conn)

        active_rows = _fetch_active_jobs(
            conn,
            since=None if full_sync else effective_since,
            limit=active_limit,
            offset=active_offset,
            include_changed_hash=include_changed_hash,
        )
        inactive_rows = _fetch_inactive_jobs(conn) if include_inactive else []

        headers = {"X-API-Key": settings.firebase_sync_api_key}
        sent_batches = 0
        skipped_ids: list[str] = []

        def _push(rows: list[dict[str, Any]], label: str) -> int:
            """Push a row-set in batches; accumulate skipped ids. Returns batches sent."""
            nonlocal skipped_ids
            serialized = [_serialize_job(r) for r in rows]
            batches = list(_batched(serialized, settings.firebase_sync_batch_size))
            n_sent = 0
            for index, batch in enumerate(batches, start=1):
                _delivered, batch_skipped = _post_jobs_batch(
                    url=settings.firebase_sync_url,
                    headers=headers,
                    collection=settings.firebase_sync_collection,
                    mode=mode,
                    jobs=batch,
                    timeout=settings.firebase_sync_timeout_seconds,
                )
                n_sent += 1
                if batch_skipped:
                    skipped_ids.extend(batch_skipped)
                logger.info(
                    "Synced Firebase {} batch {}/{} ({} jobs)",
                    label, index, len(batches), len(batch),
                )
            return n_sent

        # seam_dead_sync-1 fix: push INACTIVE (deactivation / dead-flip) rows
        # FIRST. They carry the status='active'->'inactive' transitions that
        # REMOVE confirmed-dead jobs from the live matcher; if an active batch
        # raised mid-loop they used to never POST (they were concatenated last),
        # leaving dead jobs served as 404 matches (the 1,792-dead incident on
        # the propagation side). Pushing them first guarantees deactivations
        # propagate even if a later active batch fails. The inactive push is in
        # its own try so an inactive failure does not block the active push
        # either — each half is independently best-effort, and any raise still
        # leaves the watermark un-advanced so the window retries next run.
        if inactive_rows:
            sent_batches += _push(inactive_rows, "inactive")

        # If any active batch push raises, it propagates WITHOUT advancing the
        # watermark, so the next run re-covers this window (self-healing).
        sent_batches += _push(active_rows, "active")

        # seam_embed_sync-1 fix: a doc the receiver terminally rejected
        # (bisect-skip, _post_jobs_batch returned it in skipped_ids) was NOT
        # delivered — it must stay re-selectable. Exclude skipped ids from BOTH
        # the synced-hash ledger AND the watermark so a rejected matchable row
        # is not silently recorded as synced and dropped forever.
        skipped_set = set(skipped_ids)
        if skipped_set:
            logger.error(
                "Firebase sync: {} doc(s) terminally rejected and NOT delivered "
                "(excluded from ledger + watermark, will retry next run): {}",
                len(skipped_set), sorted(skipped_set)[:20],
            )
        delivered_active = [
            r for r in active_rows
            if not (isinstance(r, dict) and r.get("job_id") in skipped_set)
        ]

        # Fix #4: record the content_hash shipped for each DELIVERED active row
        # so a content_hash-only change is not re-sent next run unless it
        # changes again. Skipped for staged backfills (paged window).
        if include_changed_hash and delivered_active:
            _record_synced_hashes(conn, delivered_active)

        # Advance the durable watermark to the max embedded_at we actually
        # DELIVERED (never past a skipped row). If the only rows in this window
        # were skipped, the watermark does not advance and they retry next run.
        new_watermark = _max_embedded_at(delivered_active)
        if watermark_active and new_watermark is not None:
            _advance_sync_watermark(conn, new_watermark)

    stats = {
        "active_jobs": len(active_rows),
        "inactive_jobs": len(inactive_rows),
        "synced": len(active_rows) + len(inactive_rows) - len(skipped_set),
        "skipped_docs": len(skipped_set),
        "batches": sent_batches,
    }
    logger.info("Firebase sync complete: {}", stats)
    return stats
