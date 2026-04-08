"""Fast path: LLM gap-fill + embedding only (skip slow ATS parsing).

Usage:
    cd wekruit-matching && source .venv/bin/activate
    python scripts/embed_all.py
"""
import sys
import time

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

sys.path.insert(0, "src")

from wekruit_matching.db.connection import get_connection
from wekruit_matching.embedding.worker import embed_pending
from wekruit_matching.enrichment.worker import enrich_pending


def _db_stats():
    with get_connection() as conn:
        r = conn.execute(
            """
            SELECT count(*) AS total,
                   count(enriched_at) AS enriched,
                   count(embedded_at) AS embedded
            FROM jobs WHERE status = 'active'
            """
        ).fetchone()
        t = max(r["total"], 1)
        logger.info(
            "Enriched={}/{} ({:.0f}%) | Embedded={}/{} ({:.0f}%)",
            r["enriched"], r["total"], 100 * r["enriched"] / t,
            r["embedded"], r["total"], 100 * r["embedded"] / t,
        )


def main():
    start = time.monotonic()
    logger.info("=== Fast Path: LLM Gap-Fill + Embedding ===")
    _db_stats()

    # Stage 1: LLM gap-fill
    logger.info("--- LLM gap-fill (Qwen3-8B, free) ---")
    t0 = time.monotonic()
    total_enriched = total_failed = 0
    rnd = 0
    while True:
        rnd += 1
        with get_connection() as conn:
            stats = enrich_pending(conn)
        total_enriched += stats["enriched"]
        total_failed += stats["failed"]
        if stats["enriched"] == 0 and stats["failed"] == 0:
            break
        logger.info("LLM round {}: +{} enriched, +{} failed", rnd, stats["enriched"], stats["failed"])
    logger.info("LLM done in {:.1f}m: {} enriched, {} failed", (time.monotonic() - t0) / 60, total_enriched, total_failed)

    # Stage 2: Embedding (loop until done)
    logger.info("--- Embedding (OpenAI text-embedding-3-small) ---")
    t0 = time.monotonic()
    total_embedded = total_embed_failed = 0
    rnd = 0
    while True:
        rnd += 1
        with get_connection() as conn:
            stats = embed_pending(conn)
        total_embedded += stats["embedded"]
        total_embed_failed += stats["failed"]
        if stats["embedded"] == 0 and stats["failed"] == 0:
            break
        logger.info("Embed round {}: +{} embedded, +{} failed (total: {})", rnd, stats["embedded"], stats["failed"], total_embedded)
    logger.info("Embedding done in {:.1f}m: {} embedded, {} failed", (time.monotonic() - t0) / 60, total_embedded, total_embed_failed)

    logger.info("=== Complete in {:.1f}m ===", (time.monotonic() - start) / 60)
    _db_stats()


if __name__ == "__main__":
    main()
