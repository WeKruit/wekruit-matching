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
import time
from collections import defaultdict
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

# Re-attempt window for *recoverable* Stage 2b failures (P7-F gating fix).
# Jobs whose previous fetch failed transiently (5xx, connection error,
# timeout, Firecrawl outage) become eligible after this many days. Permanent
# failures (404 / Job not found) carry ``permanent_404 = TRUE`` and never
# re-enter the queue. Mirror of ENRICH_STALE_DAYS at Stage 2c (worker.py).
STAGE2B_STALE_DAYS = 7


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
    """Return True if ``exc`` indicates a permanently-dead URL.

    Permanent: HTTP 404 from any ATS / Firecrawl / Workday fetcher; also
    LookupError raised when the page genuinely has no job posting (Workday
    CXS endpoint not discoverable). Both signal the listing is gone — retry
    won't help.

    Recoverable (NOT permanent): HTTP 5xx, connection errors, timeouts,
    parse errors, anything else. These get the staleness retry window.
    """
    # httpx.HTTPStatusError carries .response.status_code
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            return exc.response.status_code == 404
        except AttributeError:
            return False
    # Workday CXS discovery failure — page exposed no job (employer pulled it
    # or path wasn't a real job page). Treated as permanent.
    if isinstance(exc, LookupError):
        return True
    return False


def _throttle_domain(
    last_request_at: dict[str, float],
    domain: str,
    *,
    min_interval_seconds: float,
) -> None:
    """Ensure requests to the same domain are spaced out."""
    if not domain or min_interval_seconds <= 0:
        return

    now = time.monotonic()
    previous = last_request_at.get(domain)
    if previous is not None:
        remaining = min_interval_seconds - (now - previous)
        if remaining > 0:
            time.sleep(remaining)
    last_request_at[domain] = time.monotonic()


async def _search_url(row: dict, settings) -> str | None:
    """Use Firecrawl search to find a direct employer URL."""
    if not settings.firecrawl_api_key:
        return None
    return await search_canonical_job_url(
        company_name=row["company_name"],
        role_title=row["role_title"],
        api_key=settings.firecrawl_api_key,
        base_url=settings.firecrawl_base_url,
    )


async def _fetch_for_url(url: str, settings) -> tuple[AtsJobData | None, int]:
    """Fetch one job description using the correct free or paid tier."""
    classification = classify_job_url(url)
    route = classification.route
    if route is FetchRoute.GREENHOUSE:
        return fetch_greenhouse_job(url), 0
    if route is FetchRoute.LEVER:
        return fetch_lever_job(url), 0
    if route is FetchRoute.ASHBY:
        return fetch_ashby_job(url), 0
    if route is FetchRoute.WORKDAY:
        # Skip CXS discovery (45s timeout, ~95% failure rate) — go straight to Firecrawl
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
    """Persist a successful JD fetch result."""
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


def _write_failure(conn, job_id: str, *, permanent_404: bool = False) -> None:
    """Persist a failed JD attempt.

    ``permanent_404`` defaults False (recoverable) so the row re-enters the
    queue after STAGE2B_STALE_DAYS days. Set True only when ``_is_permanent_404``
    confirms a 404 / dead URL — those are excluded from the queue forever.
    """
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


def run_jd_enrichment(
    *,
    conn=None,
    settings=None,
    batch_size: int = 500,
    domain_min_interval: float = 0.5,
    dry_run: bool = False,
) -> dict:
    """Process the JD enrichment queue in batches of at most 500 rows."""
    if conn is None:
        with get_connection() as owned_conn:
            return run_jd_enrichment(
                conn=owned_conn,
                settings=settings,
                batch_size=batch_size,
                domain_min_interval=domain_min_interval,
                dry_run=dry_run,
            )

    settings = settings or get_settings()
    if not hasattr(settings, "firecrawl_api_key"):
        settings = SimpleNamespace(
            firecrawl_api_key="",
            firecrawl_base_url="https://api.firecrawl.dev",
            **settings.__dict__,
        )

    source_counts: dict[str, int] = defaultdict(int)
    failed_by_source: dict[str, int] = defaultdict(int)
    processed = failed = skipped = credits_used = 0
    last_request_at: dict[str, float] = {}
    effective_batch_size = min(batch_size, 500)

    while True:
        rows = conn.execute(
            f"""
            SELECT job_id, company_name, role_title, primary_url, ats_apply_url
            FROM jobs
            WHERE status = 'active'
              AND (job_description IS NULL OR job_description = '')
              -- P7-L (2026-05-08): include rows where the only fetchable URL is ats_apply_url.
              -- Most jobright-sourced rows keep primary_url=https://jobright.ai/... even after
              -- paBackfillAtsUrlsBatch resolves a real employer URL into ats_apply_url. The
              -- old WHERE primary_url IS NOT NULL AND primary_url NOT LIKE 'jobright.ai/%%'
              -- starved Stage 2b of 29,497 active rows. Now: at least one of the two URLs
              -- must be a real (non-jobright) ATS link.
              AND (
                (primary_url IS NOT NULL AND primary_url NOT LIKE 'https://jobright.ai/%%')
                OR (ats_apply_url IS NOT NULL AND ats_apply_url NOT LIKE 'https://jobright.ai/%%')
              )
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

        for row in rows:
            # P7-L: pick the URL most likely to yield a JD. Jobright primary_url
            # cannot be fetched (we'd just hit the aggregator). Prefer ats_apply_url
            # whenever primary_url is jobright-style; otherwise use primary_url.
            primary_url_raw = row.get("primary_url") or ""
            ats_apply_url_raw = row.get("ats_apply_url") or ""
            primary_is_jobright = primary_url_raw.startswith("https://jobright.ai/")
            ats_is_real = bool(ats_apply_url_raw) and not ats_apply_url_raw.startswith(
                "https://jobright.ai/"
            )
            if primary_is_jobright and ats_is_real:
                original_url = ats_apply_url_raw
            elif primary_url_raw and not primary_is_jobright:
                original_url = primary_url_raw
            elif ats_is_real:
                original_url = ats_apply_url_raw
            else:
                # No fetchable URL on this row — should not happen given the
                # SELECT clause but defensive: skip without burning a fetch.
                skipped += 1
                continue

            target_url = original_url
            route = classify_job_url(original_url).route
            if _is_aggregator_url(original_url):
                resolved = asyncio.run(_search_url(row, settings))
                if resolved:
                    target_url = resolved
                    route = classify_job_url(target_url).route

            source_counts[route.value] += 1
            processed += 1

            if dry_run:
                continue

            domain = urlparse(normalize_job_url(target_url)).netloc.lower()
            _throttle_domain(
                last_request_at,
                domain,
                min_interval_seconds=domain_min_interval,
            )

            try:
                result, spend = asyncio.run(_fetch_for_url(target_url, settings))
                credits_used += spend
                if result is None:
                    if route is FetchRoute.FIRECRAWL and not settings.firecrawl_api_key:
                        skipped += 1
                        continue
                    # ``result is None`` means the fetcher succeeded transport-wise
                    # but extracted no JD (e.g. Firecrawl extract returned empty).
                    # That's a recoverable signal — content might surface next run.
                    _write_failure(conn, row["job_id"], permanent_404=False)
                    failed_by_source[route.value] += 1
                    failed += 1
                    continue
                _write_success(conn, row["job_id"], result)
            except Exception as exc:
                permanent = _is_permanent_404(exc)
                logger.warning(
                    "JD enrichment failed for {} (permanent_404={}): {}",
                    row["job_id"],
                    permanent,
                    exc,
                )
                _write_failure(conn, row["job_id"], permanent_404=permanent)
                failed_by_source[route.value] += 1
                failed += 1

        if not dry_run:
            conn.commit()

    return {
        "processed": processed,
        "failed": failed,
        "skipped": skipped,
        "credits_used": credits_used,
        "sources": dict(source_counts),
        "failed_by_source": dict(failed_by_source),
        "dry_run": dry_run,
    }
