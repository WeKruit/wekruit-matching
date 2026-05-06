"""Cold start: run all pipeline stages in sequence with proper looping.

Stages:
  1. JobRight parsing (loops internally) — ~2K jobs missing skills
  2. ATS JD enrichment (loops internally) — non-JobRight jobs without JD
  3. LLM gap-fill (loops until 0 pending) — Qwen3-8B, free
  4. Embedding (loops until 0 pending) — OpenAI text-embedding-3-small

Phase 66 (2026-05-06): URL resolution stage removed — migrated to wekruit-pa
Cloud Function `paBackfillAtsUrlsBatch` (hourly).

Usage:
    cd wekruit-matching
    source .venv/bin/activate
    python scripts/cold_start.py
"""
import sys
import time

from loguru import logger

logger.remove()
logger.add(sys.stderr, level="INFO", format="{time:HH:mm:ss} | {level:<7} | {message}")

# Ensure project is importable
sys.path.insert(0, "src")

from wekruit_matching.db.connection import get_connection
from wekruit_matching.embedding.worker import embed_pending
from wekruit_matching.enrichment.worker import enrich_pending
from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment
from wekruit_matching.scraper.enrich_from_jobright import enrich_all_jobs


def _db_stats():
    """Print current coverage stats."""
    with get_connection() as conn:
        r = conn.execute(
            """
            SELECT
                count(*) AS total,
                count(job_description) AS with_jd,
                count(CASE WHEN required_skills != ARRAY[]::text[] THEN 1 END) AS with_skills,
                count(sponsorship) AS with_sponsor,
                count(enriched_at) AS enriched,
                count(embedded_at) AS embedded,
                count(ats_apply_url) AS with_ats_url
            FROM jobs WHERE status = 'active'
            """
        ).fetchone()
        t = max(r["total"], 1)
        logger.info(
            "Coverage: JD={}/{} ({:.0f}%) | Skills={}/{} ({:.0f}%) | "
            "Enriched={}/{} ({:.0f}%) | Embedded={}/{} ({:.0f}%) | "
            "ATS URL={}/{} ({:.0f}%)",
            r["with_jd"], r["total"], 100 * r["with_jd"] / t,
            r["with_skills"], r["total"], 100 * r["with_skills"] / t,
            r["enriched"], r["total"], 100 * r["enriched"] / t,
            r["embedded"], r["total"], 100 * r["embedded"] / t,
            r["with_ats_url"], r["total"], 100 * r["with_ats_url"] / t,
        )


def main():
    start = time.monotonic()
    logger.info("=== Cold Start Pipeline ===")
    _db_stats()

    # --- Stage 1: JobRight parsing (free, loops internally) ---
    logger.info("--- Stage 1: JobRight __NEXT_DATA__ parsing ---")
    t0 = time.monotonic()
    with get_connection() as conn:
        jr_stats = enrich_all_jobs(conn, max_workers=8, batch_size=50)
    logger.info("JobRight parsing done in {:.1f}m: {}", (time.monotonic() - t0) / 60, jr_stats)
    _db_stats()

    # --- Stage 2: ATS JD enrichment (loops internally) ---
    logger.info("--- Stage 2: ATS JD enrichment (Greenhouse/Lever/Ashby/Workday) ---")
    t0 = time.monotonic()
    with get_connection() as conn:
        jd_stats = run_jd_enrichment(conn=conn)
    logger.info("ATS JD enrichment done in {:.1f}m: {}", (time.monotonic() - t0) / 60, jd_stats)

    # --- Stage 3 (was Stage 4): LLM gap-fill (loop until 0 pending) ---
    # Phase 66: previous Stage 3 (URL resolution) removed — wekruit-pa CF
    # paBackfillAtsUrlsBatch handles it on an hourly cadence.
    logger.info("--- Stage 3: LLM gap-fill (Qwen3-8B, free) ---")
    t0 = time.monotonic()
    total_enriched = total_failed = 0
    round_num = 0
    while True:
        round_num += 1
        with get_connection() as conn:
            stats = enrich_pending(conn)
        total_enriched += stats["enriched"]
        total_failed += stats["failed"]
        if stats["enriched"] == 0 and stats["failed"] == 0:
            break
        logger.info(
            "LLM round {}: enriched={} failed={} (cumulative: {} enriched, {} failed)",
            round_num, stats["enriched"], stats["failed"], total_enriched, total_failed,
        )
    logger.info(
        "LLM gap-fill done in {:.1f}m: {} enriched, {} failed",
        (time.monotonic() - t0) / 60, total_enriched, total_failed,
    )

    # --- Stage 4 (was Stage 5): Embedding (loop until 0 pending) ---
    logger.info("--- Stage 4: Embedding (OpenAI text-embedding-3-small) ---")
    t0 = time.monotonic()
    total_embedded = total_embed_failed = 0
    round_num = 0
    while True:
        round_num += 1
        with get_connection() as conn:
            stats = embed_pending(conn)
        total_embedded += stats["embedded"]
        total_embed_failed += stats["failed"]
        if stats["embedded"] == 0 and stats["failed"] == 0:
            break
        logger.info(
            "Embed round {}: embedded={} failed={} (cumulative: {} embedded, {} failed)",
            round_num, stats["embedded"], stats["failed"], total_embedded, total_embed_failed,
        )
    logger.info(
        "Embedding done in {:.1f}m: {} embedded, {} failed",
        (time.monotonic() - t0) / 60, total_embedded, total_embed_failed,
    )

    # --- Final stats ---
    duration = time.monotonic() - start
    logger.info("=== Cold Start Complete in {:.1f}m ===", duration / 60)
    _db_stats()


if __name__ == "__main__":
    main()
