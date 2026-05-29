"""Enrichment worker orchestrator.

Reads all unenriched jobs from the DB, classifies each with Claude Haiku,
and writes enrichment results back.

Standalone CLI usage:
    uv run python -m wekruit_matching.enrichment.run

Or import and call programmatically:
    from wekruit_matching.enrichment.run import enrich_all
    stats = enrich_all()
"""
import sys

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.enrichment.worker import (
    backfill_seniority,
    count_null_seniority_active,
    enrich_pending,
)


def enrich_all() -> dict[str, int]:
    """Run enrichment for all pending jobs.

    Steps:
      1. ``enrich_pending`` — LLM classify the gap-fill queue (industry,
         company_size, required_skills, sponsorship).
      2. ``backfill_seniority`` — deterministic, source-agnostic regex fill of
         ``jobs.seniority_level`` (canonical intern|entry|mid|senior vocab) for
         every active row where it is NULL. Added 2026-05-29: 80.8% of active
         jobs had NULL seniority_level, which job_sync was syncing as-is to the
         live matcher; this had no zero-cost writer before.
      3. ``count_null_seniority_active`` — runtime gate; logs a WARNING if any
         active rows remain NULL after the backfill.

    Returns the enrich_pending stats dict, augmented with
    ``seniority_backfilled`` and ``seniority_null_after``.
    """
    with get_connection() as conn:
        stats = enrich_pending(conn)
        try:
            stats["seniority_backfilled"] = backfill_seniority(conn)
            stats["seniority_null_after"] = count_null_seniority_active(conn)
        except Exception as exc:  # defensive — backfill must never crash the run
            logger.warning("seniority backfill failed: {}", exc)
            stats["seniority_backfilled"] = -1
        return stats


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting enrichment worker run")
    stats = enrich_all()
    logger.info("Enrichment complete. Stats: {}", stats)
