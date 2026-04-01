"""Phase 16 URL resolution.

resolve_simplify_jobs() covers SimplifyJobs rows (RESOLVE-02): copies
primary_url directly to ats_apply_url when the URL is a known ATS link
(Greenhouse, Lever, Ashby, Workday). No network calls required.

resolve_via_slug_registry() covers JobRight rows (RESOLVE-03, implemented
in Plan 02).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from wekruit_matching.pipeline.url_classifier import FetchRoute, classify_job_url

if TYPE_CHECKING:
    from wekruit_matching.scraper.slug_registry import SlugRegistry


@dataclass(frozen=True, slots=True)
class ResolveResult:
    """Resolution outcome for a single job."""

    job_id: str
    company_name: str
    role_title: str
    resolved_url: str | None
    source: str | None  # "simplify_copy" | "slug_registry" | "serper" | None
    tier: str | None


def resolve_simplify_jobs(
    conn,
    *,
    batch_size: int = 500,
) -> dict:
    """Resolve SimplifyJobs rows by copying primary_url to ats_apply_url.

    For every active SimplifyJobs job (source_repo NOT LIKE 'jobright%') that
    has no ats_apply_url yet, classify the primary_url. If the route is NOT
    FIRECRAWL (i.e., it is a known ATS URL — Greenhouse, Lever, Ashby, or
    Workday), copy the primary_url directly to ats_apply_url. No network call
    required — zero-cost resolution.

    Skip condition: route == FetchRoute.FIRECRAWL — URL is unknown/aggregator,
    cannot be promoted blindly.

    Args:
        conn: Active psycopg3 connection (caller owns lifecycle/commit).
        batch_size: Max rows per DB round-trip. Capped at 500 per pipeline
            convention.

    Returns:
        dict with keys: resolved, skipped, errors.
    """
    effective_batch_size = min(batch_size, 500)
    resolved = skipped = errors = 0

    while True:
        rows = conn.execute(
            """
            SELECT job_id, company_name, role_title, primary_url
            FROM jobs
            WHERE status = 'active'
              AND source_repo NOT LIKE 'jobright%%'
              AND ats_apply_url IS NULL
              AND primary_url IS NOT NULL
              AND primary_url != ''
            ORDER BY first_seen_at DESC
            LIMIT %(limit)s
            """,
            {"limit": effective_batch_size},
        ).fetchall()

        if not rows:
            break

        batch_resolved = 0

        for row in rows:
            primary_url: str = row["primary_url"] or ""
            job_id: str = row["job_id"]

            try:
                classification = classify_job_url(primary_url)
            except Exception as exc:
                logger.warning(
                    "url_resolver: classify failed for job={} url={}: {}",
                    job_id,
                    primary_url,
                    exc,
                )
                errors += 1
                continue

            if classification.route == FetchRoute.FIRECRAWL:
                # Unknown or aggregator URL — skip, cannot copy blindly
                skipped += 1
                continue

            # Direct ATS URL — copy to ats_apply_url
            conn.execute(
                """
                UPDATE jobs
                SET ats_apply_url = %(url)s
                WHERE job_id = %(job_id)s
                """,
                {"url": primary_url, "job_id": job_id},
            )
            resolved += 1
            batch_resolved += 1

        if batch_resolved > 0:
            conn.commit()

    return {"resolved": resolved, "skipped": skipped, "errors": errors}


def resolve_via_slug_registry(
    conn,
    registry: "SlugRegistry",
    *,
    batch_size: int = 500,
) -> dict:
    """Resolve JobRight jobs via slug registry (RESOLVE-03).

    Implemented in Plan 02. This stub satisfies the interface contract so
    Plan 01 callers can import and wire the function without breakage.

    Args:
        conn: Active psycopg3 connection.
        registry: Populated SlugRegistry (see scraper/slug_registry.py).
        batch_size: Max rows per DB round-trip.

    Returns:
        dict with keys: resolved, skipped, errors.
    """
    return {"resolved": 0, "skipped": 0, "errors": 0}
