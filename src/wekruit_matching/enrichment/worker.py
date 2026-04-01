"""Enrichment worker: reads unenriched jobs, calls classify_job, writes results.

Content-hash gating: only processes jobs where enriched_at IS NULL.
When a job's content_hash changes, upsert.py clears enriched_at — so
changed jobs automatically re-enter the enrichment queue.

Per-job failure isolation: a single classify_job exception or DB write
error logs a warning and increments the failed counter — the batch continues.
"""
from datetime import UTC, datetime

import psycopg
from loguru import logger

from wekruit_matching.enrichment.classifier import classify_job
from wekruit_matching.models.job import Job


def _utcnow() -> datetime:
    return datetime.now(UTC)


def enrich_pending(conn: psycopg.Connection) -> dict[str, int]:
    """Classify active jobs that are missing a JD or skills (ENRICH-01 gap-fill).

    Query: SELECT ... FROM jobs WHERE status = 'active'
             AND enriched_at IS NULL
             AND (job_description IS NULL OR required_skills = ARRAY[]::text[])

    ENRICH-01: LLM enrichment is a gap-fill step — only runs when upstream
    parsing left the job without a JD or without skills.

    For each job:
      1. Call classify_job(job) — returns EnrichmentResult (never raises)
      2. UPDATE jobs SET industry, company_size, required_skills, sponsorship, enriched_at
      3. commit() after each successful write

    Returns {"enriched": N, "failed": M, "skipped": 0}
    """
    rows = conn.execute(
        """
        SELECT
          job_id,
          source_repo,
          company_name,
          role_title,
          location_raw,
          content_hash,
          job_description,
          required_skills
        FROM jobs
        WHERE status = 'active'
          AND enriched_at IS NULL
          AND (
            job_description IS NULL
            OR required_skills = ARRAY[]::text[]
          )
        ORDER BY first_seen_at ASC
        LIMIT 500
        """
    ).fetchall()

    if not rows:
        logger.info("No gap-fill jobs found — nothing to do")
        return {"enriched": 0, "failed": 0, "skipped": 0}

    logger.info("Found {} gap-fill job(s) to classify (missing JD or skills)", len(rows))
    enriched = failed = 0

    for row in rows:
        job = Job(
            job_id=row["job_id"],
            source_repo=row["source_repo"],
            company_name=row["company_name"],
            role_title=row["role_title"],
            location_raw=row["location_raw"] or "",
            content_hash=row["content_hash"],
            job_description=row["job_description"],
        )
        try:
            result = classify_job(job)
            conn.execute(
                """
                UPDATE jobs SET
                    industry        = %(industry)s,
                    company_size    = %(company_size)s,
                    required_skills = %(required_skills)s,
                    sponsorship     = %(sponsorship)s,
                    enriched_at     = %(enriched_at)s
                WHERE job_id = %(job_id)s
                """,
                {
                    "job_id": job.job_id,
                    "industry": result.industry,
                    "company_size": result.company_size,
                    "required_skills": result.required_skills,
                    "sponsorship": result.sponsorship,
                    "enriched_at": _utcnow(),
                },
            )
            conn.commit()
            enriched += 1
            logger.debug(
                "Enriched {}: industry={} company_size={} sponsorship={}",
                job.job_id[:8],
                result.industry,
                result.company_size,
                result.sponsorship,
            )
        except Exception as exc:
            failed += 1
            logger.warning("Failed to enrich job {}: {}", job.job_id[:8], exc)
            # Roll back any partial writes so the connection is clean for the next job
            conn.rollback()
            # Continue to next job — per-job isolation

    logger.info("Enrichment complete: enriched={} failed={}", enriched, failed)
    return {"enriched": enriched, "failed": failed, "skipped": 0}
