"""Embedding only — loop until all enriched jobs are embedded.

Skips LLM gap-fill. Just embeds the ~40K jobs that already have enriched_at.

Usage:
    cd wekruit-matching && source .venv/bin/activate
    python scripts/embed_only.py
"""
import sys
import time

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

sys.path.insert(0, "src")

from wekruit_matching.db.connection import get_connection
from wekruit_matching.embedding.worker import embed_pending


def main():
    start = time.monotonic()

    with get_connection() as conn:
        r = conn.execute("""
            SELECT count(*) as pending FROM jobs
            WHERE status='active' AND embedded_at IS NULL AND enriched_at IS NOT NULL
        """).fetchone()
    logger.info("=== Embedding {} pending jobs ===", r["pending"])

    total_embedded = total_failed = 0
    rnd = 0
    while True:
        rnd += 1
        with get_connection() as conn:
            stats = embed_pending(conn)
        total_embedded += stats["embedded"]
        total_failed += stats["failed"]
        if stats["embedded"] == 0 and stats["failed"] == 0:
            break
        elapsed = time.monotonic() - start
        rate = total_embedded / elapsed * 60 if elapsed > 0 else 0
        logger.info(
            "Round {}: +{} embedded, +{} failed | Total: {} ({:.0f}/min, {:.1f}m elapsed)",
            rnd, stats["embedded"], stats["failed"],
            total_embedded, rate, elapsed / 60,
        )

    elapsed = time.monotonic() - start
    logger.info("=== Done: {} embedded, {} failed in {:.1f}m ===", total_embedded, total_failed, elapsed / 60)


if __name__ == "__main__":
    main()
