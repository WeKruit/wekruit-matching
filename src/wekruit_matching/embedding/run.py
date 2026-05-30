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


# ``embed_pending`` embeds at most LIMIT (3000) rows per call. A single daily
# pass therefore left a backlog whenever more than that became embeddable in a
# run (observed 2026-05-30: 4,370 embeddable rows left unembedded after one
# pass → ~18% of active never reached the matcher until a manual loop drained
# them). Loop until the queue is empty so coverage cannot silently sit below
# 100%. Bounded by ``max_passes`` so a persistent per-row failure can't loop
# forever.
_MAX_EMBED_PASSES = 30  # 30 * 3000 = 90k headroom; real backlog is < 30k


def embed_all(*, max_passes: int = _MAX_EMBED_PASSES) -> dict[str, int]:
    """Run embedding for ALL pending jobs, looping until the queue drains.

    ``embed_pending`` is LIMIT-bounded per call; this drains the full backlog so
    a daily run leaves zero embeddable-but-unembedded rows.

    Returns aggregated {"embedded": N, "failed": M, "skipped": 0, "passes": P}.
    """
    total_embedded = total_failed = passes = 0
    while passes < max_passes:
        with get_connection() as conn:
            stats = embed_pending(conn)
        passes += 1
        n = stats.get("embedded", 0)
        total_embedded += n
        total_failed += stats.get("failed", 0)
        if n == 0:
            break
        logger.info("embed_all pass {} embedded {} (total {})", passes, n, total_embedded)
    else:
        logger.warning(
            "embed_all hit max_passes={} — embeddable backlog may remain; "
            "investigate per-row embed failures",
            max_passes,
        )
    return {
        "embedded": total_embedded,
        "failed": total_failed,
        "skipped": 0,
        "passes": passes,
    }


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting embedding worker run")
    stats = embed_all()
    logger.info("Embedding complete. Stats: {}", stats)
