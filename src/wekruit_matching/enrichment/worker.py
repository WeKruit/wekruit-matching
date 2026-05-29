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

import os
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

# 2026-05-20 — empty-skills alert threshold.
#
# Active rows with NULL/empty required_skills are an observability metric.
# 2026-05-21 follow-up: classify_job now returns None when JD is missing
# (anti-hallucination guard) — the worker treats that as "do not write"
# so ``enriched_at`` stays NULL and the row stays eligible for next run.
# Result: empty-skills rows in the active set are now only those where
# Stage 2b still hasn't landed a JD. We DO emit a WARNING when the
# threshold is crossed so ops sees it in the daily logs.
#
# Override via ``ALERT_EMPTY_SKILLS_THRESHOLD`` env var (int). Default 100
# is conservative — well under the typical post-enrichment empty-skills
# tail and well over the noise floor.
ALERT_EMPTY_SKILLS_THRESHOLD = int(os.environ.get("ALERT_EMPTY_SKILLS_THRESHOLD", "100"))


def count_active_empty_skills(conn: psycopg.Connection) -> int:
    """Return the count of active rows whose required_skills is NULL or empty.

    Idempotent read-only query. Used by both the in-pipeline alert hook
    and the standalone ``scripts/alert-empty-skills.py`` cron script.
    """
    row = conn.execute(
        """
        SELECT COUNT(*)::int AS n
        FROM jobs
        WHERE status = 'active'
          AND (
            required_skills IS NULL
            OR cardinality(required_skills) = 0
          )
        """
    ).fetchone()
    if row is None:
        return 0
    # psycopg returns dict-like rows; tolerate both shapes for safety.
    return int(row["n"]) if isinstance(row, dict) else int(row[0])


def alert_if_empty_skills_exceeds_threshold(
    conn: psycopg.Connection,
    *,
    threshold: int = ALERT_EMPTY_SKILLS_THRESHOLD,
) -> int:
    """Log a WARNING when active rows missing required_skills exceed threshold.

    Returns the count (regardless of whether the alert fired) so callers can
    aggregate the value into a pipeline summary. Idempotent — safe to call
    many times per run, the threshold check is pure.
    """
    count = count_active_empty_skills(conn)
    if count >= threshold:
        logger.warning(
            "alert.empty_skills.active count={} threshold={} — investigate enrichment quality",
            count,
            threshold,
        )
    else:
        logger.info("alert.empty_skills.active count={} threshold={} (ok)", count, threshold)
    return count


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


def _process_one_job(row: dict) -> tuple[str, str, Exception | None]:
    """Worker-thread payload: classify one job and write the result.

    Each worker acquires its own pooled connection — psycopg Connection
    objects are NOT thread-safe, so we cannot share the caller's conn.

    Returns ``(outcome, job_id_short, exc_if_any)`` where ``outcome`` is:
      * ``"enriched"`` — classify_job returned a result, UPDATE committed
      * ``"skipped_no_jd"`` — JD too short / missing, classify_job returned
        None, no UPDATE issued. ``enriched_at`` stays NULL so the row
        re-enters the queue next pipeline run (when Stage 2b may have
        landed a JD).
      * ``"failed"`` — classify_job raised, or the UPDATE write failed.

    Never raises — every exception is captured and reported back so the
    main thread can keep counting failures without losing siblings.
    """
    job = _row_to_job(row)
    short_id = job.job_id[:8]
    try:
        result = classify_job(job)
    except Exception as exc:
        return "failed", short_id, exc

    if result is None:
        # Anti-hallucination guard fired — JD missing/too-short.
        # Skip UPDATE so ``enriched_at`` stays NULL and the row stays in
        # the queue for the next run.
        return "skipped_no_jd", short_id, None

    # 2026-05-28 lockout fix. A JD-bearing job that classify_job returns with
    # ZERO skills is an extraction MISS, not a finished job. The old code
    # stamped enriched_at unconditionally → the row (a) hid behind the 7-day
    # staleness gate and (b) failed the embed gate (cardinality(skills)>0).
    # Net effect at audit: 22,564 active JD-bearing jobs locked out of the
    # matching pool. Leave enriched_at NULL on empty skills so the row stays
    # eligible and retries next run (classify_job is cheap gpt-5.x-nano and,
    # verified live, extracts skills fine on retry once JD is present).
    has_skills = bool(result.required_skills)
    enriched_at_val = _utcnow() if has_skills else None
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
                    "enriched_at": enriched_at_val,
                },
            )
            conn.commit()
    except Exception as exc:
        return "failed", short_id, exc

    logger.debug(
        "Enriched {}: industry={} company_size={} sponsorship={}",
        short_id,
        result.industry,
        result.company_size,
        result.sponsorship,
    )
    return "enriched", short_id, None


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
        LIMIT 3000
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
    enriched = failed = skipped_no_jd = 0
    counter_lock = threading.Lock()  # guards loguru ordering of progress logs

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_process_one_job, row) for row in rows]
        for fut in as_completed(futures):
            try:
                outcome, short_id, exc = fut.result()
            except Exception as inner_exc:  # _process_one_job should never raise, defensive
                with counter_lock:
                    failed += 1
                logger.warning("Worker raised unexpectedly: {}", inner_exc)
                continue

            if outcome == "enriched":
                with counter_lock:
                    enriched += 1
            elif outcome == "skipped_no_jd":
                with counter_lock:
                    skipped_no_jd += 1
            else:
                with counter_lock:
                    failed += 1
                logger.warning("Failed to enrich job {}: {}", short_id, exc)

    logger.info(
        "Enrichment complete: enriched={} skipped_no_jd={} failed={}",
        enriched, skipped_no_jd, failed,
    )

    # 2026-05-20 — empty-skills alert. Counts BOTH the pre-existing tail
    # (rows the enrichment queue didn't get to this run) and the new
    # rows that completed enrichment with empty skills (LLM had no JD to
    # extract from, or extracted nothing matching the schema). The log
    # line is picked up by macmini launchd stderr; ops dashboards filter
    # on "alert.empty_skills.active".
    try:
        empty_skills_count = alert_if_empty_skills_exceeds_threshold(conn)
    except Exception as exc:  # defensive — alerting must never crash the pipeline
        logger.warning("empty-skills alert query failed: {}", exc)
        empty_skills_count = -1

    return {
        "enriched": enriched,
        "failed": failed,
        "skipped": skipped_no_jd,
        "skipped_no_jd": skipped_no_jd,
        "empty_skills_active": empty_skills_count,
    }
