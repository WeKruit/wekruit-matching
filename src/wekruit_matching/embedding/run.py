"""Embedding worker orchestrator.

Reads all enriched unembedded jobs from the DB, generates embeddings via OpenAI
text-embedding-3-small, and writes vectors back to the jobs table.

Standalone CLI usage:
    uv run python -m wekruit_matching.embedding.run

Or import and call programmatically:
    from wekruit_matching.embedding.run import embed_all
    stats = embed_all()
"""
import sys

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.embedding.worker import embed_pending


def embed_all() -> dict[str, int]:
    """Run embedding for all pending jobs.

    Returns {"embedded": N, "failed": M, "skipped": 0}
    """
    with get_connection() as conn:
        return embed_pending(conn)


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting embedding worker run")
    stats = embed_all()
    logger.info("Embedding complete. Stats: {}", stats)
