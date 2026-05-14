"""Upsert pipeline for scraped job listings.

Writes Job objects to the jobs table using ON CONFLICT (job_id) DO UPDATE.
The idempotent upsert pattern ensures re-running the scraper on unchanged data
produces zero DB writes beyond last_seen_at bookkeeping.

Stale marking (mark_stale_jobs) sets status='inactive' for jobs that
disappeared from the README — it never deletes rows, preserving history
for Phase 3 enrichment context.

P7-K (2026-05-09) — Postgres dead tombstone
-------------------------------------------
After 0007 added ``dead`` / ``dead_confirmed_at`` columns, this module is
the gate that prevents the dead-URL infinite-loop scenario described in
that migration. On every batch:

1. Pre-pass: SELECT existing dead state for the batch's job_ids
2. Recovery pass: any row with ``dead=true AND dead_confirmed_at < NOW()
   - INTERVAL '90 days'`` is reset (dead=false, dead_confirmed_at=NULL)
   to allow ONE retry. Capped at 100 rows per pipeline run so a flood of
   stale tombstones can't undo all of them in one pass.
3. Skip pass: any row still ``dead=true`` (within 30/90d window OR with
   NULL dead_confirmed_at as legacy backfill) is *removed from the input
   set*. The normal UPSERT below then sees fewer rows and never resets
   their status from 'inactive' → 'active'.

Logs ``pa.scraper.skipped_dead_jobs {count: N}`` on every run for
ops dashboards.
"""
from collections.abc import Collection
from datetime import UTC, datetime

import psycopg
from loguru import logger

from wekruit_matching.models.job import Job


def _utcnow() -> datetime:
    return datetime.now(UTC)


_UPSERT_BATCH_SIZE = 500

# P7-K constants
_DEAD_RETRY_AGE_DAYS = 90        # tombstone older than this => allow one retry
_DEAD_RETRY_MAX_PER_RUN = 100    # safety cap so we don't undo all tombstones at once


def _filter_dead_tombstoned(
    jobs: list[Job],
    conn: psycopg.Connection,
) -> tuple[list[Job], int, int]:
    """Strip dead-tombstoned jobs from ``jobs`` and recover any past 90d.

    Returns: (filtered_jobs, skipped_count, retried_count)

    Behaviour (matches P9 directive):
      * dead=true AND dead_confirmed_at < NOW() - 90d : retry path
        - reset dead=false, dead_confirmed_at=NULL (capped at 100/run)
        - keep job in the filtered list (normal UPSERT proceeds)
      * dead=true AND dead_confirmed_at >= NOW() - 90d : skip
        (covers the 30-day skip-window + the 30-90d hold)
      * dead=true AND dead_confirmed_at IS NULL (legacy backfill) : skip
        — we don't know how old the tombstone is, treat as recent
      * dead=false / NULL : pass through unchanged

    Always uses a single SELECT + (optional) UPDATE; no per-row queries.
    Empty / never-seen-before batches return immediately.
    """
    if not jobs:
        return jobs, 0, 0

    job_ids = [j.job_id for j in jobs]
    rows = conn.execute(
        """
        SELECT job_id, dead, dead_confirmed_at
        FROM jobs
        WHERE job_id = ANY(%(ids)s)
          AND dead IS TRUE
        """,
        {"ids": job_ids},
    ).fetchall()
    if not rows:
        return jobs, 0, 0

    # Partition: which dead rows are eligible for the 90d retry path?
    retry_ids: list[str] = []
    skip_ids: set[str] = set()
    cutoff = _utcnow().replace(tzinfo=UTC)
    for r in rows:
        confirmed_at = r["dead_confirmed_at"]
        if confirmed_at is None:
            # Legacy / Stage-0 backfill with no timestamp. Treat as recent
            # to be safe — single 90d retry can fire next time once the
            # liveness sweep re-confirms.
            skip_ids.add(r["job_id"])
            continue
        # Normalize tz-naive timestamps from psycopg to UTC for comparison
        if confirmed_at.tzinfo is None:
            confirmed_at = confirmed_at.replace(tzinfo=UTC)
        age_days = (cutoff - confirmed_at).total_seconds() / 86400
        if age_days >= _DEAD_RETRY_AGE_DAYS:
            retry_ids.append(r["job_id"])
        else:
            skip_ids.add(r["job_id"])

    # Cap retries per run (safety: 100 stale tombstones don't all reset together)
    if len(retry_ids) > _DEAD_RETRY_MAX_PER_RUN:
        # Excess retries get demoted back into the skip set this run; they'll
        # be eligible again next pipeline run.
        skip_ids.update(retry_ids[_DEAD_RETRY_MAX_PER_RUN:])
        retry_ids = retry_ids[:_DEAD_RETRY_MAX_PER_RUN]

    # Reset the retry-eligible rows so the subsequent UPSERT is allowed to
    # re-activate them. dead=false + dead_confirmed_at=NULL means "we'll
    # let the next liveness sweep tell us if this URL is really dead".
    if retry_ids:
        conn.execute(
            """
            UPDATE jobs
            SET dead = FALSE,
                dead_confirmed_at = NULL
            WHERE job_id = ANY(%(ids)s)
            """,
            {"ids": retry_ids},
        )
        conn.commit()

    if skip_ids:
        filtered = [j for j in jobs if j.job_id not in skip_ids]
        logger.info(
            "pa.scraper.skipped_dead_jobs count={} retried={}",
            len(skip_ids),
            len(retry_ids),
        )
    else:
        filtered = jobs
        if retry_ids:
            logger.info(
                "pa.scraper.skipped_dead_jobs count=0 retried={}",
                len(retry_ids),
            )

    return filtered, len(skip_ids), len(retry_ids)


def upsert_jobs(jobs: list[Job], conn: psycopg.Connection) -> dict[str, int]:
    """Batch upsert Job records into the jobs table.

    Uses UNNEST-based batch INSERT ... ON CONFLICT for 50-100x speedup
    over row-by-row. Processes in chunks of 500 for Supabase timeout safety.

    P10-audit fix (2026-05-06): persist seniority_level, role_function,
    sources, and job_description on insert (previously dropped silently —
    scrapers set them but they never reached the DB).

    P7-K (2026-05-09): pre-filter dead-tombstoned jobs (defense-in-depth
    against the dead-URL infinite-loop). See ``_filter_dead_tombstoned``.

    Returns: {"inserted": N, "updated": N, "unchanged": N,
              "skipped_dead": N, "dead_retried": N}
    """
    if not jobs:
        return {
            "inserted": 0, "updated": 0, "unchanged": 0,
            "skipped_dead": 0, "dead_retried": 0,
        }

    # P7-K — strip dead-tombstoned URLs before any UPSERT touches them.
    # Done once for the whole call rather than per-batch because the
    # tombstone set is small (typically <1% of inputs) and one SELECT
    # covering all job_ids is cheaper than N batched ones.
    jobs, skipped_dead, dead_retried = _filter_dead_tombstoned(jobs, conn)

    inserted = updated = unchanged = 0

    if not jobs:
        # Whole input was tombstoned. Skip the UPSERT loop entirely.
        logger.info(
            "Upserted 0 jobs: 0 inserted, 0 updated, 0 unchanged "
            "(skipped_dead={} dead_retried={})",
            skipped_dead, dead_retried,
        )
        return {
            "inserted": 0, "updated": 0, "unchanged": 0,
            "skipped_dead": skipped_dead, "dead_retried": dead_retried,
        }

    for i in range(0, len(jobs), _UPSERT_BATCH_SIZE):
        batch = jobs[i : i + _UPSERT_BATCH_SIZE]
        now = _utcnow()

        # Collect existing hashes for this batch to detect changes
        batch_ids = [j.job_id for j in batch]
        existing = {}
        if batch_ids:
            rows = conn.execute(
                "SELECT job_id, content_hash FROM jobs WHERE job_id = ANY(%(ids)s)",
                {"ids": batch_ids},
            ).fetchall()
            existing = {r["job_id"]: r["content_hash"] for r in rows}

        # Batch upsert using cursor.executemany (psycopg3)
        conn.cursor().executemany(
            """
            INSERT INTO jobs (
                job_id, source_repo, company_name, role_title,
                primary_url, location_raw, date_posted_raw,
                status, first_seen_at, last_seen_at, content_hash,
                industry, company_size, required_skills, sponsorship,
                enriched_at,
                seniority_level, role_function, sources,
                job_description
            ) VALUES (
                %(job_id)s, %(source_repo)s, %(company_name)s, %(role_title)s,
                %(primary_url)s, %(location_raw)s, %(date_posted_raw)s,
                'active', %(now)s, %(now)s, %(content_hash)s,
                %(industry)s, %(company_size)s, %(required_skills)s, %(sponsorship)s,
                %(enriched_at)s,
                %(seniority_level)s, %(role_function)s, %(sources)s,
                %(job_description)s
            )
            ON CONFLICT (job_id) DO UPDATE SET
                location_raw    = EXCLUDED.location_raw,
                date_posted_raw = EXCLUDED.date_posted_raw,
                last_seen_at    = EXCLUDED.last_seen_at,
                status          = 'active',
                content_hash    = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN EXCLUDED.content_hash
                    ELSE jobs.content_hash
                END,
                enriched_at     = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN NULL
                    ELSE jobs.enriched_at
                END,
                embedding       = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN NULL
                    ELSE jobs.embedding
                END,
                embedding_model = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN NULL
                    ELSE jobs.embedding_model
                END,
                embedded_at     = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN NULL
                    ELSE jobs.embedded_at
                END,
                -- P10-audit fix: keep seniority_level / role_function /
                -- sources / job_description fresh on every upsert. These
                -- are derived from role_title which is stable per job_id,
                -- so overwriting with EXCLUDED is always safe.
                seniority_level = COALESCE(EXCLUDED.seniority_level, jobs.seniority_level),
                role_function   = CASE
                    WHEN cardinality(EXCLUDED.role_function) > 0
                    THEN EXCLUDED.role_function
                    ELSE jobs.role_function
                END,
                sources         = CASE
                    WHEN cardinality(EXCLUDED.sources) > 0
                    THEN (
                        SELECT array_agg(DISTINCT s)
                        FROM unnest(jobs.sources || EXCLUDED.sources) AS s
                    )
                    ELSE jobs.sources
                END,
                job_description = COALESCE(EXCLUDED.job_description, jobs.job_description)
            """,
            [
                {
                    "job_id": job.job_id,
                    "source_repo": job.source_repo,
                    "company_name": job.company_name,
                    "role_title": job.role_title,
                    "primary_url": job.primary_url,
                    "location_raw": job.location_raw,
                    "date_posted_raw": job.date_posted_raw,
                    "content_hash": job.content_hash,
                    "industry": job.industry,
                    "company_size": job.company_size,
                    "required_skills": job.required_skills or [],
                    "sponsorship": job.sponsorship,
                    "enriched_at": now if job.industry else None,
                    "seniority_level": job.seniority_level,
                    "role_function": job.role_function or [],
                    "sources": job.sources or [],
                    "job_description": job.job_description,
                    "now": now,
                }
                for job in batch
            ],
        )
        conn.commit()

        # Count results from pre-fetched hashes
        for job in batch:
            if job.job_id not in existing:
                inserted += 1
            elif existing[job.job_id] != job.content_hash:
                updated += 1
            else:
                unchanged += 1

    logger.info(
        "Upserted {} jobs: {} inserted, {} updated, {} unchanged "
        "(skipped_dead={} dead_retried={})",
        len(jobs), inserted, updated, unchanged,
        skipped_dead, dead_retried,
    )

    # Carry forward ats_apply_url from recently-deactivated jobs to new active
    # rows with the same company+title. Prevents re-burning Serper credits on
    # jobs that just got a new job_id from the source repo.
    if inserted > 0:
        recovered = conn.execute(
            """
            UPDATE jobs a
            SET ats_apply_url = b.ats_apply_url,
                jd_fetch_source = b.jd_fetch_source
            FROM (
                SELECT DISTINCT ON (company_name, role_title)
                       company_name, role_title, ats_apply_url, jd_fetch_source
                FROM jobs
                WHERE status = 'inactive'
                  AND ats_apply_url IS NOT NULL
                  AND last_seen_at > NOW() - INTERVAL '30 days'
                ORDER BY company_name, role_title, last_seen_at DESC
            ) b
            WHERE a.status = 'active'
              AND a.ats_apply_url IS NULL
              AND a.company_name = b.company_name
              AND a.role_title = b.role_title
            """,
        ).rowcount
        conn.commit()
        if recovered > 0:
            logger.info("Carried forward {} ats_apply_url from inactive jobs", recovered)

    return {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "skipped_dead": skipped_dead,
        "dead_retried": dead_retried,
    }


_STALE_BATCH_SIZE = 5000


def mark_stale_jobs(
    seen_ids: Collection[str],
    source_repo: str,
    conn: psycopg.Connection,
) -> int:
    """Mark active jobs from source_repo as inactive if their job_id is not in seen_ids.

    Called after upsert to deactivate listings that disappeared from the README.
    Never deletes rows — preserves history for enrichment context.
    Scoped to source_repo — stale marking for one repo never affects another.

    For large ID sets (>5000), uses a two-step approach to avoid statement
    timeouts on Supabase's pooler: first collects active IDs, then batches
    updates on the smaller stale subset.

    Returns: count of rows marked inactive
    """
    if not seen_ids:
        # Edge case: all jobs disappeared — mark all active as inactive
        result = conn.execute(
            """
            UPDATE jobs
            SET status = 'inactive'
            WHERE source_repo = %(source_repo)s AND status = 'active'
            """,
            {"source_repo": source_repo},
        )
        conn.commit()
        count = result.rowcount
        logger.info("Marked {} stale jobs inactive for repo {}", count, source_repo)
        return count

    seen_set = set(seen_ids)
    total_marked = 0

    if len(seen_set) <= _STALE_BATCH_SIZE:
        # Small set — single NOT IN query is fast enough
        result = conn.execute(
            """
            UPDATE jobs
            SET status = 'inactive'
            WHERE source_repo = %(source_repo)s
              AND status = 'active'
              AND NOT (job_id = ANY(%(seen_ids)s))
            """,
            {"source_repo": source_repo, "seen_ids": list(seen_set)},
        )
        total_marked = result.rowcount
        conn.commit()
    else:
        # Large set — collect active IDs first, then batch-update stale ones
        logger.info(
            "Large ID set ({}) for {} — using batched stale marking",
            len(seen_set), source_repo,
        )
        active_rows = conn.execute(
            """
            SELECT job_id FROM jobs
            WHERE source_repo = %(source_repo)s AND status = 'active'
            """,
            {"source_repo": source_repo},
        ).fetchall()

        stale_ids = [r["job_id"] for r in active_rows if r["job_id"] not in seen_set]
        logger.info("Found {} stale jobs to deactivate", len(stale_ids))

        for i in range(0, len(stale_ids), _STALE_BATCH_SIZE):
            batch = stale_ids[i : i + _STALE_BATCH_SIZE]
            result = conn.execute(
                """
                UPDATE jobs
                SET status = 'inactive'
                WHERE job_id = ANY(%(stale_ids)s)
                """,
                {"stale_ids": batch},
            )
            total_marked += result.rowcount
            conn.commit()

    logger.info("Marked {} stale jobs inactive for repo {}", total_marked, source_repo)
    return total_marked


def mark_specific_ids_inactive(
    stale_ids: set[str],
    source_repo: str,
    conn: psycopg.Connection,
) -> int:
    """Mark a specific set of job_ids as inactive within source_repo.

    Inverse semantics of ``mark_stale_jobs``:
      * ``mark_stale_jobs(seen_ids, ...)``  marks everything NOT in seen_ids inactive.
      * ``mark_specific_ids_inactive(stale_ids, ...)`` marks EXACTLY stale_ids inactive.

    Used by the pure-diff jobright path (``JOBRIGHT_USE_GIT_DELTA=1``): the
    ``-`` rows in HEAD~1..HEAD give us the canonical removal set; we don't
    need to scan the full README to deduce it.

    Returns
    -------
    Count of rows actually flipped from active -> inactive (no-op if already inactive).
    """
    if not stale_ids:
        return 0
    result = conn.execute(
        """
        UPDATE jobs
        SET status = 'inactive', last_seen_at = %s
        WHERE source_repo = %s
          AND job_id = ANY(%s)
          AND status = 'active'
        """,
        (_utcnow(), source_repo, list(stale_ids)),
    )
    return result.rowcount
