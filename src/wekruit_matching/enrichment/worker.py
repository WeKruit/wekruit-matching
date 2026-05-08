"""Enrichment worker: reads unenriched jobs, calls classify_job, writes results.

Content-hash gating: jobs are eligible when ``enriched_at IS NULL`` *or* when
the previous classification is older than the staleness window
(``ENRICH_STALE_DAYS``). When a job's content_hash changes, upsert.py clears
``enriched_at`` — so changed jobs immediately re-enter the queue.

Why the staleness window matters (P7-E, 2026-05-08):
  Earlier behaviour stamped ``enriched_at = NOW()`` even when the LLM only
  saw company+role (no JD) and returned ``industry='unknown', skills=[]``.
  Once stamped, those jobs *never* re-entered the queue — even when Stage 2b
  later landed a real JD. Postgres reality at fix-time: 37,521 active jobs
  / 29,165 missing JD / 330 in queue. 29,157 of the missing-JD jobs already
  had ``enriched_at`` set, so they were stuck. The staleness window lets
  the upstream JD pipeline land a JD within ENRICH_STALE_DAYS days before
  we re-spend a Qwen3-8B call on the same job.

Per-job failure isolation: a single classify_job exception or DB write
error logs a warning and increments the failed counter — the batch continues.

PARALLELISM (2026-05-08):
The previous sequential `for row in rows` loop ran ~30s/job (Qwen3-8B HTTP),
hitting the 4hr SIGALRM kill at ~500 jobs/day. We now fan out classification
across a ThreadPoolExecutor (default 10 workers). Each worker grabs its OWN
connection from the psycopg pool (psycopg connections are NOT thread-safe),
runs classify_job + UPDATE in isolation, and commits independently.
Wall-time target: 10x reduction (250min -> ~25min).

Connection-pool note: get_pool() is configured with max_size=20, so 10
workers + the main-thread reader connection stays well under the cap.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

import psycopg
from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.enrichment.classifier import classify_job
from wekruit_matching.models.job import Job

# Re-attempt window: jobs with stale enriched_at that still lack JD or skills
# become eligible after this many days. 7d is a starting point — short enough
# that newly-landed JDs get classified within a week, long enough that we
# don't burn LLM credits weekly on permanently empty jobs (e.g. 1-line listings
# whose careers page never loads). Tunable; revisit with telemetry.
ENRICH_STALE_DAYS = 7


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _row_to_job(row: dict) -> Job:
    return Job(
        job_id=row["job_id"],
        source_repo=row["source_repo"],
        company_name=row["company_name"],
        role_title=row["role_title"],
        location_raw=row["location_raw"] or "",
        content_hash=row["content_hash"],
        job_description=row["job_description"],
    )


def _process_one_job(row: dict) -> tuple[bool, str, Exception | None]:
    """Worker-thread payload: classify one job and write the result.

    Each worker acquires its own pooled connection — psycopg Connection
    objects are NOT thread-safe, so we cannot share the caller's conn.

    Returns (success, job_id_short, exc_if_any). Never raises — every
    exception is captured and reported back so the main thread can keep
    counting failures without losing siblings.
    """
    job = _row_to_job(row)
    short_id = job.job_id[:8]
    try:
        result = classify_job(job)
    except Exception as exc:
        return False, short_id, exc

    try:
        with get_connection() as conn:
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
    except Exception as exc:
        return False, short_id, exc

    logger.debug(
        "Enriched {}: industry={} company_size={} sponsorship={}",
        short_id,
        result.industry,
        result.company_size,
        result.sponsorship,
    )
    return True, short_id, None


def enrich_pending(
    conn: psycopg.Connection,
    *,
    max_workers: int = 10,
) -> dict[str, int]:
    """Classify active jobs that are missing a JD or skills (ENRICH-01 gap-fill).

    Query: SELECT ... FROM jobs WHERE status = 'active'
             AND (enriched_at IS NULL
                  OR enriched_at < NOW() - INTERVAL '7 days')
             AND (job_description IS NULL OR required_skills = ARRAY[]::text[])

    ENRICH-01: LLM enrichment is a gap-fill step — only runs when upstream
    parsing left the job without a JD or without skills.

    Two-clause gating (P7-E fix):
      Clause 1 (entry):     enriched_at IS NULL OR enriched_at < NOW() - 7d
      Clause 2 (data gap):  job_description IS NULL OR required_skills = []

    Both must hold. Fully-enriched jobs (have JD and skills) never re-enter
    the queue, regardless of age. Stuck jobs (have ``enriched_at`` stamped
    from an empty pass but still missing JD or skills) become eligible after
    7 days — giving Stage 2b time to land a real JD before we re-classify.

    Concurrency (P7-A): jobs are classified in parallel using a
    ThreadPoolExecutor of size `max_workers` (default 10). Each worker grabs
    its own pooled connection for the UPDATE — `conn` (the caller's
    connection) is used only for the initial SELECT.

    Args:
        conn: psycopg connection used for the SELECT. Workers do NOT share
            this connection; they each get their own from the pool.
        max_workers: ThreadPoolExecutor size. Pass 1 for sequential behaviour
            (useful for tests or debugging). Default 10.

    For each job:
      1. Call classify_job(job) — returns EnrichmentResult (never raises)
      2. UPDATE jobs SET industry, company_size, required_skills, sponsorship, enriched_at
      3. commit() after each successful write

    Returns {"enriched": N, "failed": M, "skipped": 0}
    """
    rows = conn.execute(
        f"""
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
          AND (
            enriched_at IS NULL
            OR enriched_at < NOW() - INTERVAL '{ENRICH_STALE_DAYS} days'
          )
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

    logger.info(
        "Found {} gap-fill job(s) to classify (missing JD or skills) — fanning out across {} worker(s)",
        len(rows),
        max_workers,
    )
    enriched = failed = 0
    counter_lock = threading.Lock()  # guards loguru ordering of progress logs

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_process_one_job, row) for row in rows]
        for fut in as_completed(futures):
            try:
                success, short_id, exc = fut.result()
            except Exception as inner_exc:  # _process_one_job should never raise, defensive
                with counter_lock:
                    failed += 1
                logger.warning("Worker raised unexpectedly: {}", inner_exc)
                continue

            if success:
                with counter_lock:
                    enriched += 1
            else:
                with counter_lock:
                    failed += 1
                logger.warning("Failed to enrich job {}: {}", short_id, exc)

    logger.info("Enrichment complete: enriched={} failed={}", enriched, failed)
    return {"enriched": enriched, "failed": failed, "skipped": 0}
