"""Upsert pipeline for scraped job listings.

Writes Job objects to the jobs table using ON CONFLICT (job_id) DO UPDATE.
The idempotent upsert pattern ensures re-running the scraper on unchanged data
produces zero DB writes beyond last_seen_at bookkeeping.

Stale marking (mark_stale_jobs) sets status='inactive' for jobs that
disappeared from the README — it never deletes rows, preserving history
for Phase 3 enrichment context.
"""
from collections.abc import Collection
from datetime import UTC, datetime

import psycopg
from loguru import logger

from wekruit_matching.models.job import Job


def _utcnow() -> datetime:
    return datetime.now(UTC)


_UPSERT_BATCH_SIZE = 500


def upsert_jobs(jobs: list[Job], conn: psycopg.Connection) -> dict[str, int]:
    """Batch upsert Job records into the jobs table.

    Uses UNNEST-based batch INSERT ... ON CONFLICT for 50-100x speedup
    over row-by-row. Processes in chunks of 500 for Supabase timeout safety.

    Returns: {"inserted": N, "updated": N, "unchanged": N}
    """
    if not jobs:
        return {"inserted": 0, "updated": 0, "unchanged": 0}

    inserted = updated = unchanged = 0

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
                enriched_at
            ) VALUES (
                %(job_id)s, %(source_repo)s, %(company_name)s, %(role_title)s,
                %(primary_url)s, %(location_raw)s, %(date_posted_raw)s,
                'active', %(now)s, %(now)s, %(content_hash)s,
                %(industry)s, %(company_size)s, %(required_skills)s, %(sponsorship)s,
                %(enriched_at)s
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
                END
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
        "Upserted {} jobs: {} inserted, {} updated, {} unchanged",
        len(jobs), inserted, updated, unchanged,
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

    return {"inserted": inserted, "updated": updated, "unchanged": unchanged}


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
