"""Stage 2b orchestrator for ATS JD enrichment.

Two-clause SELECT gating (P7-F, 2026-05-08):

  Clause 1 (entry): ``jd_fetch_attempted_at IS NULL``
                    OR (``jd_fetch_source = 'failed'``
                        AND COALESCE(permanent_404, FALSE) = FALSE
                        AND ``jd_fetch_attempted_at < NOW() - 7d``)

  Clause 2 (data gap): ``job_description IS NULL OR job_description = ''``

Both must hold. Successfully-fetched jobs (have JD) never re-enter regardless
of age. Permanent-404 jobs (employer pulled the listing) are excluded entirely.
Recoverable failures (Firecrawl down, Workday 5xx, connection timeout) become
eligible after STAGE2B_STALE_DAYS days, giving upstream services time to
recover before we re-spend a fetch.

PARALLELISM (P7-M2, 2026-05-09):
The previous sequential `for row in rows` loop ran ~4.6s/job (Firecrawl HTTP +
Workday CXS discovery + ATS API). With 5K+ jobs eligible after the P7-L URL
fix, sequential drain = 14 daily runs. We now fan out fetches across a
ThreadPoolExecutor (default 10 workers). Each worker grabs its OWN connection
from the psycopg pool (psycopg connections are NOT thread-safe), runs
_fetch_for_url + UPDATE in isolation, and commits independently. Wall-time
target: 10x reduction.

Connection-pool note: get_pool() is configured with max_size=20, so 10 workers
+ the main-thread reader connection stays well under the cap. Mirror of P7-A's
pattern at Stage 2c (worker.py).

Why a 7-day staleness window: short enough that real outages (Firecrawl was
down 5+ weeks in early-2026 — a transient like that needs slack to recover)
don't burn through retry attempts in one day; long enough that we don't burn
LLM credits weekly on permanently-empty rows. Tunable via STAGE2B_STALE_DAYS
module constant; mirror of P7-E's ENRICH_STALE_DAYS at Stage 2c.

Why a boolean ``permanent_404`` rather than a 3-value enum: additive boolean
is a cheaper migration (one column, defaults FALSE) and ``success`` is
already implicit in row state (``job_description`` populated, ``jd_fetch_source``
non-failed). An enum would duplicate that signal. NULL-safety via
COALESCE(permanent_404, FALSE) handles any rows missed by the default
backfill.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
from loguru import logger

from wekruit_matching.config import get_settings
from wekruit_matching.db.connection import get_connection
from wekruit_matching.pipeline.ats_enricher import (
    AtsJobData,
    fetch_ashby_job,
    fetch_greenhouse_job,
    fetch_lever_job,
)
from wekruit_matching.pipeline.firecrawl_enricher import (
    fetch_firecrawl_job,
    fetch_workday_job,
    search_canonical_job_url,
)
from wekruit_matching.pipeline.url_classifier import FetchRoute, classify_job_url, normalize_job_url

# Re-attempt window for *recoverable* Stage 2b failures.
#
# Lowered 7 → 1 (2026-05-20, matching-quality launch blocker):
# the 6,888 NULL-JD active-pool backlog at launch eve was bottlenecked by the
# stale window. With STAGE2B_STALE_DAYS=1, transient Firecrawl/Workday/Lever
# 5xx outages get one retry per day instead of one per week, draining the
# backlog far faster. ``permanent_404=TRUE`` rows are still excluded forever,
# so the lowered window only re-enters recoverable failures.
STAGE2B_STALE_DAYS = 1


_AGGREGATOR_HOSTS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "simplyhired.com",
)


def _is_aggregator_url(url: str) -> bool:
    hostname = urlparse(normalize_job_url(url)).netloc.lower()
    return any(host in hostname for host in _AGGREGATOR_HOSTS)


def _is_permanent_404(exc: BaseException) -> bool:
    """Return True if ``exc`` indicates a permanently-dead URL."""
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return exc.response.status_code == 404
        except AttributeError:
            return False
    if isinstance(exc, LookupError):
        return True
    return False


def _throttle_domain(
    last_request_at: dict[str, float],
    domain: str,
    *,
    min_interval_seconds: float,
    lock: threading.Lock | None = None,
) -> None:
    """Ensure requests to the same domain are spaced out.

    Thread-safe variant: when `lock` is provided (parallel mode), the read +
    sleep + write of last_request_at[domain] runs under the lock so two
    workers can't both observe "no recent request" and both fire instantly.
    """
    if not domain or min_interval_seconds <= 0:
        return

    if lock is not None:
        with lock:
            now = time.monotonic()
            previous = last_request_at.get(domain)
            if previous is not None:
                remaining = min_interval_seconds - (now - previous)
                if remaining > 0:
                    time.sleep(remaining)
            last_request_at[domain] = time.monotonic()
        return

    now = time.monotonic()
    previous = last_request_at.get(domain)
    if previous is not None:
        remaining = min_interval_seconds - (now - previous)
        if remaining > 0:
            time.sleep(remaining)
    last_request_at[domain] = time.monotonic()


async def _search_url(row: dict, settings) -> str | None:
    if not settings.firecrawl_api_key:
        return None
    return await search_canonical_job_url(
        company_name=row["company_name"],
        role_title=row["role_title"],
        api_key=settings.firecrawl_api_key,
        base_url=settings.firecrawl_base_url,
    )


async def _fetch_for_url(url: str, settings) -> tuple[AtsJobData | None, int]:
    classification = classify_job_url(url)
    route = classification.route
    if route is FetchRoute.GREENHOUSE:
        return fetch_greenhouse_job(url), 0
    if route is FetchRoute.LEVER:
        return fetch_lever_job(url), 0
    if route is FetchRoute.ASHBY:
        return fetch_ashby_job(url), 0
    if route is FetchRoute.WORKDAY:
        if not settings.firecrawl_api_key:
            return None, 0
        firecrawl = await fetch_firecrawl_job(
            url,
            api_key=settings.firecrawl_api_key,
            base_url=settings.firecrawl_base_url,
        )
        return (firecrawl.job_data, firecrawl.credits_used) if firecrawl else (None, 0)
    if not settings.firecrawl_api_key:
        return None, 0
    firecrawl = await fetch_firecrawl_job(
        url,
        api_key=settings.firecrawl_api_key,
        base_url=settings.firecrawl_base_url,
    )
    return (firecrawl.job_data, firecrawl.credits_used) if firecrawl else (None, 0)


def _write_success(conn, job_id: str, result: AtsJobData) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET
          job_description = %(job_description)s,
          core_responsibilities = %(core_responsibilities)s,
          qualifications = %(qualifications)s,
          benefits = %(benefits)s,
          salary_range = %(salary_range)s,
          data_quality_score = %(data_quality_score)s,
          ats_content_hash = %(ats_content_hash)s,
          jd_fetch_source = %(jd_fetch_source)s,
          jd_fetch_attempted_at = NOW(),
          permanent_404 = FALSE
        WHERE job_id = %(job_id)s
        """,
        {
            "job_id": job_id,
            "job_description": result.description_plain,
            "core_responsibilities": result.core_responsibilities,
            "qualifications": result.qualifications,
            "benefits": result.benefits,
            "salary_range": result.salary_range,
            "data_quality_score": result.data_quality_score,
            "ats_content_hash": sha256(result.description_plain.encode("utf-8")).hexdigest(),
            "jd_fetch_source": result.source,
        },
    )


def _write_skip_no_url(conn, job_id: str) -> None:
    """Mark a row as 'no fetchable URL' so it leaves the active queue.

    Written when ``_pick_target_url`` returns None — both ``primary_url`` and
    ``ats_apply_url`` were jobright-only or empty. We set ``jd_fetch_source``
    to a distinct sentinel ('skip_no_url') so the queue-selection clause
    excludes it (it only re-admits rows where ``jd_fetch_source = 'failed'``).
    Tracks C/F can clear this column when a real ATS URL is backfilled, and
    the row will re-enter on the next run.

    ``permanent_404`` stays FALSE: the row is not permanently dead, just
    presently unfetchable from this pipeline. The jobright-native enricher
    (``enrich_from_jobright.py``) is the proper path for these.
    """
    conn.execute(
        """
        UPDATE jobs
        SET
          jd_fetch_source = 'skip_no_url',
          jd_fetch_attempted_at = NOW(),
          permanent_404 = FALSE
        WHERE job_id = %(job_id)s
        """,
        {"job_id": job_id},
    )


def _write_failure(conn, job_id: str, *, permanent_404: bool = False) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET
          jd_fetch_source = %(jd_fetch_source)s,
          jd_fetch_attempted_at = NOW(),
          permanent_404 = %(permanent_404)s
        WHERE job_id = %(job_id)s
        """,
        {
            "job_id": job_id,
            "jd_fetch_source": "failed",
            "permanent_404": permanent_404,
        },
    )


def _pick_target_url(row: dict) -> str | None:
    """Pick the URL most likely to yield a JD.

    Jobright primary_url cannot be fetched (we'd just hit the aggregator).
    Prefer ats_apply_url whenever primary_url is jobright-style; otherwise
    use primary_url. Returns None when no fetchable URL exists (caller should
    treat as skipped).
    """
    primary_url_raw = row.get("primary_url") or ""
    ats_apply_url_raw = row.get("ats_apply_url") or ""
    primary_is_jobright = primary_url_raw.startswith("https://jobright.ai/")
    ats_is_real = bool(ats_apply_url_raw) and not ats_apply_url_raw.startswith(
        "https://jobright.ai/"
    )
    if primary_is_jobright and ats_is_real:
        return ats_apply_url_raw
    if primary_url_raw and not primary_is_jobright:
        return primary_url_raw
    if ats_is_real:
        return ats_apply_url_raw
    return None


@contextmanager
def _default_connection_factory():
    """Default per-worker connection factory: pool-acquired psycopg conn."""
    with get_connection() as conn:
        yield conn


def _process_one_job(
    row: dict,
    *,
    settings,
    domain_min_interval: float,
    last_request_at: dict[str, float],
    domain_lock: threading.Lock,
    dry_run: bool,
    write_conn,
) -> dict:
    """Process one job: classify URL, fetch JD, persist.

    Pure worker payload — never raises. Returns a dict with counters the
    caller aggregates under counter_lock.

    `write_conn` is the psycopg connection to use for UPDATE + commit. In
    parallel mode this is a per-worker pooled connection; in sequential
    mode (max_workers=1) it is the caller's conn (no per-job commit so
    caller can batch).
    """
    out = {
        "processed": 0,
        "failed": 0,
        "skipped": 0,
        "credits_used": 0,
        "route": None,  # FetchRoute.value or None
        "failed_route": None,
    }

    original_url = _pick_target_url(row)
    if original_url is None:
        # No fetchable URL (typically jobright-only). Mark the row so it leaves
        # the queue — without this the row re-enters every run forever once the
        # SQL filter no longer excludes jobright-only docs (see SELECT clause).
        out["skipped"] = 1
        if not dry_run:
            try:
                _write_skip_no_url(write_conn, row["job_id"])
            except Exception as write_exc:
                logger.warning(
                    "Could not persist skip_no_url for {}: {}",
                    row["job_id"],
                    write_exc,
                )
        return out

    target_url = original_url
    route = classify_job_url(original_url).route
    if _is_aggregator_url(original_url):
        try:
            resolved = asyncio.run(_search_url(row, settings))
        except Exception as exc:
            logger.warning("Aggregator URL search failed for {}: {}", row["job_id"], exc)
            resolved = None
        if resolved:
            target_url = resolved
            route = classify_job_url(target_url).route

    out["route"] = route.value
    out["processed"] = 1

    if dry_run:
        return out

    domain = urlparse(normalize_job_url(target_url)).netloc.lower()
    _throttle_domain(
        last_request_at,
        domain,
        min_interval_seconds=domain_min_interval,
        lock=domain_lock,
    )

    try:
        result, spend = asyncio.run(_fetch_for_url(target_url, settings))
        out["credits_used"] = spend
        if result is None:
            if route is FetchRoute.FIRECRAWL and not settings.firecrawl_api_key:
                out["processed"] = 0
                out["skipped"] = 1
                return out
            _write_failure(write_conn, row["job_id"], permanent_404=False)
            out["failed_route"] = route.value
            out["failed"] = 1
            return out
        _write_success(write_conn, row["job_id"], result)
    except Exception as exc:
        permanent = _is_permanent_404(exc)
        logger.warning(
            "JD enrichment failed for {} (permanent_404={}): {}",
            row["job_id"],
            permanent,
            exc,
        )
        try:
            _write_failure(write_conn, row["job_id"], permanent_404=permanent)
        except Exception as write_exc:  # defensive — don't crash sibling workers
            logger.warning("Could not persist failure for {}: {}", row["job_id"], write_exc)
        out["failed_route"] = route.value
        out["failed"] = 1

    return out


def run_jd_enrichment(
    *,
    conn=None,
    settings=None,
    batch_size: int = 500,
    domain_min_interval: float = 0.5,
    dry_run: bool = False,
    max_workers: int = 1,  # Sequential — parallel signal-timeout broken (loop tick #5 finding)
    connection_factory=None,
) -> dict:
    """Process the JD enrichment queue in batches of at most 500 rows.

    Concurrency (P7-M2):
      - max_workers > 1: parallel mode. Each worker acquires its own pooled
        psycopg connection via `connection_factory` (defaults to get_connection),
        runs _fetch_for_url + UPDATE, and commits independently. The caller's
        `conn` is used only for the SELECT query.
      - max_workers == 1: sequential mode. Writes go through `conn`; caller's
        commit batches per-page. Preserves legacy semantics for tests.

    Args:
        conn: psycopg connection used for the SELECT and (in sequential mode)
            UPDATE. Workers in parallel mode do NOT share this connection.
        settings: optional override for get_settings().
        batch_size: max rows per SELECT page (capped at 500).
        domain_min_interval: minimum seconds between fetches against the same
            host. Throttle is shared across workers via a lock.
        dry_run: when True, classify routes and count work but skip network +
            DB writes.
        max_workers: ThreadPoolExecutor size. Default 10. Set to 1 for
            sequential behaviour (tests, debugging).
        connection_factory: context-manager factory yielding a psycopg
            connection for worker writes. Defaults to get_connection. Tests
            inject a fake here.
    """
    if conn is None:
        with get_connection() as owned_conn:
            return run_jd_enrichment(
                conn=owned_conn,
                settings=settings,
                batch_size=batch_size,
                domain_min_interval=domain_min_interval,
                dry_run=dry_run,
                max_workers=max_workers,
                connection_factory=connection_factory,
            )

    settings = settings or get_settings()
    if not hasattr(settings, "firecrawl_api_key"):
        settings = SimpleNamespace(
            firecrawl_api_key="",
            firecrawl_base_url="https://api.firecrawl.dev",
            **settings.__dict__,
        )

    if connection_factory is None:
        connection_factory = _default_connection_factory

    source_counts: dict[str, int] = defaultdict(int)
    failed_by_source: dict[str, int] = defaultdict(int)
    processed = failed = skipped = credits_used = 0
    last_request_at: dict[str, float] = {}
    domain_lock = threading.Lock()
    counter_lock = threading.Lock()
    # Cap raised 500 → 5000 (2026-05-20, matching-quality launch blocker):
    # at the 6,888-doc backlog, a 500-row cap forced 14 paginated SELECTs per
    # run with the same per-row work in sequential mode. 5000 in-memory rows
    # is cheap; the cost is per-fetch network/Firecrawl latency, not the page.
    # When parallelism (max_workers > 1) is re-enabled the cap unlocks
    # 50K-attempt runs (10 workers × 5K page).
    effective_batch_size = min(batch_size, 5000)
    sequential = max_workers <= 1

    def _aggregate(out: dict) -> None:
        nonlocal processed, failed, skipped, credits_used
        with counter_lock:
            processed += out["processed"]
            failed += out["failed"]
            skipped += out["skipped"]
            credits_used += out["credits_used"]
            if out["route"] is not None and out["processed"]:
                source_counts[out["route"]] += 1
            if out["failed_route"] is not None:
                failed_by_source[out["failed_route"]] += 1

    while True:
        rows = conn.execute(
            f"""
            SELECT job_id, company_name, role_title, primary_url, ats_apply_url
            FROM jobs
            WHERE status = 'active'
              AND (job_description IS NULL OR job_description = '')
              -- Pre-filter dropped 2026-05-20 (matching-quality launch blocker):
              -- the prior ``primary_url NOT LIKE jobright OR ats_apply_url NOT
              -- LIKE jobright`` clause silently hid jobright-only rows from
              -- every metric so the 6,888 NULL-JD backlog showed up as "no
              -- pending work". Now every active null-JD row is admitted; the
              -- _pick_target_url skip-path marks no-fetchable-URL rows with
              -- ``jd_fetch_source='skip_no_url'`` so they leave the queue
              -- after one pass (still re-enter when an ATS URL is backfilled
              -- and the source column is cleared by Track C / F).
              AND (
                jd_fetch_attempted_at IS NULL
                OR (
                  jd_fetch_source = 'failed'
                  AND COALESCE(permanent_404, FALSE) = FALSE
                  AND jd_fetch_attempted_at < NOW() - INTERVAL '{STAGE2B_STALE_DAYS} days'
                )
              )
            ORDER BY first_seen_at DESC
            LIMIT %(limit)s
            """,
            {"limit": effective_batch_size},
        ).fetchall()
        if not rows:
            break

        if sequential:
            # Legacy synchronous path — writes go to the caller's conn so
            # existing tests can assert on conn.executed without injecting
            # a connection factory.
            for row in rows:
                out = _process_one_job(
                    row,
                    settings=settings,
                    domain_min_interval=domain_min_interval,
                    last_request_at=last_request_at,
                    domain_lock=domain_lock,
                    dry_run=dry_run,
                    write_conn=conn,
                )
                _aggregate(out)
            if not dry_run:
                conn.commit()
            continue

        # Parallel path: fan out across max_workers, each with its own conn.
        def _worker(row):
            if dry_run:
                # Dry-run never writes; bypass connection acquisition.
                return _process_one_job(
                    row,
                    settings=settings,
                    domain_min_interval=domain_min_interval,
                    last_request_at=last_request_at,
                    domain_lock=domain_lock,
                    dry_run=True,
                    write_conn=None,
                )
            try:
                with connection_factory() as worker_conn:
                    result = _process_one_job(
                        row,
                        settings=settings,
                        domain_min_interval=domain_min_interval,
                        last_request_at=last_request_at,
                        domain_lock=domain_lock,
                        dry_run=False,
                        write_conn=worker_conn,
                    )
                    worker_conn.commit()
                    return result
            except Exception as exc:  # connection-acquisition failure
                logger.warning("Worker connection error for {}: {}", row.get("job_id"), exc)
                return {
                    "processed": 0,
                    "failed": 1,
                    "skipped": 0,
                    "credits_used": 0,
                    "route": None,
                    "failed_route": "connection_error",
                }

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_worker, row) for row in rows]
            for fut in as_completed(futures):
                try:
                    out = fut.result()
                except Exception as inner_exc:  # _worker should never raise, defensive
                    logger.warning("Worker raised unexpectedly: {}", inner_exc)
                    with counter_lock:
                        failed += 1
                    continue
                _aggregate(out)

    return {
        "processed": processed,
        "failed": failed,
        "skipped": skipped,
        "credits_used": credits_used,
        "sources": dict(source_counts),
        "failed_by_source": dict(failed_by_source),
        "dry_run": dry_run,
    }
