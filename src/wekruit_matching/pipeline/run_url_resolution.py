"""Stage 3 URL resolution orchestrator. Called by daily.py after Stage 2 parsing.

Runs two resolution passes in sequence:
  1. resolve_simplify_jobs — zero-cost copy of known ATS URLs for SimplifyJobs rows.
  2. resolve_via_slug_registry — fuzzy slug lookup + ATS title search for JobRight rows.

Both passes share the same DB connection. The slug registry is loaded once.
"""
from __future__ import annotations

import sys

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.pipeline.url_resolver import resolve_simplify_jobs, resolve_via_slug_registry
from wekruit_matching.scraper.slug_registry import load_registry


def run_url_resolution(
    *,
    conn=None,
    batch_size: int = 500,
    dry_run: bool = False,
) -> dict:
    """Orchestrate both URL resolution passes and return combined stats.

    Args:
        conn: Optional psycopg3 connection. If None, opens one via get_connection().
        batch_size: Max rows per DB round-trip (passed to each sub-function).
        dry_run: Passed through to sub-functions. Currently accepted but
            ignored by sub-functions — Plan 03 can add full dry_run support.

    Returns:
        dict with keys:
            - "simplify": stats from resolve_simplify_jobs (resolved, skipped, errors)
            - "slug_registry": stats from resolve_via_slug_registry (resolved, skipped, errors)
            - "total_resolved": sum of resolved counts from both passes
    """
    if conn is None:
        with get_connection() as owned_conn:
            return run_url_resolution(
                conn=owned_conn,
                batch_size=batch_size,
                dry_run=dry_run,
            )

    registry = load_registry()

    logger.info("url_resolution: starting SimplifyJobs pass (batch_size={})", batch_size)
    simplify_stats = resolve_simplify_jobs(conn, batch_size=batch_size)
    logger.info("url_resolution: simplify pass complete — {}", simplify_stats)

    logger.info("url_resolution: starting slug registry pass (batch_size={})", batch_size)
    registry_stats = resolve_via_slug_registry(conn, registry, batch_size=batch_size)
    logger.info("url_resolution: slug registry pass complete — {}", registry_stats)

    total_resolved = simplify_stats["resolved"] + registry_stats["resolved"]
    logger.info("url_resolution: total_resolved={}", total_resolved)

    return {
        "simplify": simplify_stats,
        "slug_registry": registry_stats,
        "total_resolved": total_resolved,
    }


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    stats = run_url_resolution()
    logger.info("URL resolution complete: {}", stats)
