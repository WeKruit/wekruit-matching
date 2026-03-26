"""Upsert pipeline for scraped job listings.

Writes Job objects to the jobs table using ON CONFLICT (job_id) DO UPDATE.
The idempotent upsert pattern ensures re-running the scraper on unchanged data
produces zero DB writes beyond last_seen_at bookkeeping.

Stale marking (mark_stale_jobs) sets status='inactive' for jobs that
disappeared from the README — it never deletes rows, preserving history
for Phase 3 enrichment context.
"""
from datetime import datetime, timezone
from typing import Collection

import psycopg
from loguru import logger

from wekruit_matching.models.job import Job


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def upsert_jobs(jobs: list[Job], conn: psycopg.Connection) -> dict[str, int]:
    """Upsert a list of Job records into the jobs table.

    Uses ON CONFLICT (job_id) DO UPDATE to handle existing rows:
    - Updates location_raw, date_posted_raw, last_seen_at always
    - Updates content_hash and sets status="active" only when content_hash changes
    - Never overwrites first_seen_at, enriched_at, or LLM-enriched fields

    Returns: {"inserted": N, "updated": N, "unchanged": N}
    """
    if not jobs:
        return {"inserted": 0, "updated": 0, "unchanged": 0}

    inserted = updated = unchanged = 0

    for job in jobs:
        now = _utcnow()
        # Use a CTE to capture the old content_hash so we can detect changes.
        # The WITH clause reads the current row (if any) before the upsert,
        # then RETURNING reports both xmax (insert vs update) and the old hash.
        result = conn.execute(
            """
            WITH old AS (
                SELECT content_hash AS old_hash
                FROM jobs
                WHERE job_id = %(job_id)s
            )
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
                END
            RETURNING
                (xmax = 0) AS was_inserted,
                (SELECT old_hash FROM old) AS old_hash,
                jobs.content_hash AS new_hash
            """,
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
            },
        )
        row = result.fetchone()
        if row is None:
            # Should never happen with RETURNING
            unchanged += 1
            continue

        if row["was_inserted"]:
            inserted += 1
        elif row["old_hash"] != row["new_hash"]:
            # Hash changed — meaningful content update
            updated += 1
        else:
            # Hash unchanged — no meaningful content change
            unchanged += 1

    conn.commit()
    logger.info(
        "Upserted {} jobs: {} inserted, {} updated, {} unchanged",
        len(jobs), inserted, updated, unchanged,
    )
    return {"inserted": inserted, "updated": updated, "unchanged": unchanged}


def mark_stale_jobs(
    seen_ids: Collection[str],
    source_repo: str,
    conn: psycopg.Connection,
) -> int:
    """Mark active jobs from source_repo as inactive if their job_id is not in seen_ids.

    Called after upsert to deactivate listings that disappeared from the README.
    Never deletes rows — preserves history for enrichment context.
    Scoped to source_repo — stale marking for one repo never affects another.

    Returns: count of rows marked inactive
    """
    if seen_ids:
        result = conn.execute(
            """
            UPDATE jobs
            SET status = 'inactive'
            WHERE source_repo = %(source_repo)s
              AND status = 'active'
              AND NOT (job_id = ANY(%(seen_ids)s))
            """,
            {"source_repo": source_repo, "seen_ids": list(seen_ids)},
        )
    else:
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
