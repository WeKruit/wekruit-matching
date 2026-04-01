"""Stage 3 URL resolution orchestrator. Called by daily.py after Stage 2 parsing.

Runs three resolution passes in sequence:
  1. resolve_simplify_jobs — zero-cost copy of known ATS URLs for SimplifyJobs rows.
  2. resolve_via_slug_registry — fuzzy slug lookup + ATS title search for JobRight rows.
  3. resolve_via_serper — Serper.dev search fallback for remaining JobRight rows
     (only runs when SERPER_API_KEY is set in config).

All passes share the same DB connection. The slug registry is loaded once.
After all passes, measures the resolution rate on the latest 1K active jobs.
"""
from __future__ import annotations

import sys

from loguru import logger

from wekruit_matching.config import get_settings
from wekruit_matching.db.connection import get_connection
from wekruit_matching.pipeline.url_resolver import (
    resolve_simplify_jobs,
    resolve_via_serper,
    resolve_via_slug_registry,
)
from wekruit_matching.scraper.slug_registry import load_registry


def run_url_resolution(
    *,
    conn=None,
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict:
    """Orchestrate all three URL resolution passes and return combined stats.

    Args:
        conn: Optional psycopg3 connection. If None, opens one via get_connection().
        batch_size: Max rows per DB round-trip (passed to each sub-function).
        dry_run: Passed through to sub-functions. Currently accepted but
            ignored by sub-functions.

    Returns:
        dict with keys:
            - "simplify": stats from resolve_simplify_jobs (resolved, skipped, errors)
            - "slug_registry": stats from resolve_via_slug_registry (resolved, skipped, errors)
            - "serper": stats from resolve_via_serper (resolved, skipped, errors, queries_used)
            - "total_resolved": sum of resolved counts from all passes
            - "resolution_rate": float 0.0–1.0 — fraction of latest 1K active jobs
              with ats_apply_url set
    """
    if conn is None:
        with get_connection() as owned_conn:
            return run_url_resolution(
                conn=owned_conn,
                batch_size=batch_size,
                dry_run=dry_run,
            )

    settings = get_settings()
    registry = load_registry()

    logger.info("url_resolution: starting SimplifyJobs pass (batch_size={})", batch_size)
    simplify_stats = resolve_simplify_jobs(conn, batch_size=batch_size)
    logger.info("url_resolution: simplify pass complete — {}", simplify_stats)

    logger.info("url_resolution: starting slug registry pass (batch_size={})", batch_size)
    registry_stats = resolve_via_slug_registry(conn, registry, batch_size=batch_size)
    logger.info("url_resolution: slug registry pass complete — {}", registry_stats)

    serper_stats: dict = {"resolved": 0, "skipped": 0, "errors": 0, "queries_used": 0}
    if settings.serper_api_key:
        logger.info("url_resolution: starting Serper.dev fallback pass")
        serper_stats = resolve_via_serper(conn, settings.serper_api_key, batch_size=batch_size)
        logger.info("url_resolution: serper pass complete — {}", serper_stats)
    else:
        logger.debug("url_resolution: SERPER_API_KEY not set — skipping Serper pass")

    total_resolved = (
        simplify_stats["resolved"] + registry_stats["resolved"] + serper_stats["resolved"]
    )
    logger.info("url_resolution: total_resolved={}", total_resolved)

    # Measure resolution rate on the latest 1K active jobs
    row = conn.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE ats_apply_url IS NOT NULL) AS resolved,
            COUNT(*) AS total
        FROM (
            SELECT ats_apply_url FROM jobs
            WHERE status = 'active'
            ORDER BY first_seen_at DESC
            LIMIT 1000
        ) sub
        """
    ).fetchone()
    resolution_rate = row["resolved"] / row["total"] if row and row["total"] else 0.0
    logger.info(
        "url_resolution: resolution_rate={:.1%} ({}/{})",
        resolution_rate,
        row["resolved"] if row else 0,
        row["total"] if row else 0,
    )

    return {
        "simplify": simplify_stats,
        "slug_registry": registry_stats,
        "serper": serper_stats,
        "total_resolved": total_resolved,
        "resolution_rate": resolution_rate,
    }


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    stats = run_url_resolution()
    logger.info("URL resolution complete: {}", stats)
