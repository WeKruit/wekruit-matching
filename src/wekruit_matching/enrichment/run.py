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
from wekruit_matching.enrichment.worker import enrich_pending


def enrich_all() -> dict[str, int]:
    """Run enrichment for all pending jobs.

    Returns {"enriched": N, "failed": M, "skipped": 0}
    """
    with get_connection() as conn:
        return enrich_pending(conn)


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting enrichment worker run")
    stats = enrich_all()
    logger.info("Enrichment complete. Stats: {}", stats)
