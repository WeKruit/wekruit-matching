"""Unified daily pipeline orchestrator.

Runs scrape -> enrich -> embed in sequence, captures stats and errors,
and sends email notifications on start and completion.

Standalone CLI usage:
    uv run python -m wekruit_matching.pipeline.daily

Replaces the fragmented daily-update.sh + inline launchd commands.
"""
import sys
import time

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.embedding.run import embed_all
from wekruit_matching.enrichment.run import enrich_all
from wekruit_matching.notifications.email import (
    send_pipeline_complete_email,
    send_pipeline_start_email,
)
from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment
from wekruit_matching.pipeline.run_url_resolution import run_url_resolution
from wekruit_matching.scraper.enrich_from_jobright import enrich_all_jobs as enrich_jobright
from wekruit_matching.scraper.run import scrape_all


def run_daily_pipeline() -> dict:
    """Execute the full daily pipeline with email notifications.

    Returns a dict with all stage stats and any errors encountered.
    """
    start = time.monotonic()
    errors: list[str] = []

    # --- Notify: start ---
    send_pipeline_start_email()

    # --- Stage 1: Scrape ---
    logger.info("=== Stage 1: Scraping ===")
    try:
        scrape_stats = scrape_all()
        logger.info("Scrape stats: {}", scrape_stats)
        for source, stats in scrape_stats.items():
            if "error" in stats:
                errors.append(f"Scrape {source}: {stats['error']}")
    except Exception as e:
        logger.error("Scraper crashed: {}", e)
        scrape_stats = {"pipeline": {"error": str(e)}}
        errors.append(f"Scraper crash: {e}")

    # --- Stage 2a: Enrich from JobRight pages (FREE — no LLM) ---
    logger.info("=== Stage 2a: JobRight Page Enrichment (free) ===")
    jobright_stats = {"enriched": 0, "failed": 0, "skills_found": 0}
    try:
        with get_connection() as conn:
            jobright_stats = enrich_jobright(conn, max_workers=8, batch_size=50)
        logger.info("JobRight enrich stats: {}", jobright_stats)
    except Exception as e:
        logger.error("JobRight enrichment crashed: {}", e)
        errors.append(f"JobRight enrichment crash: {e}")

    # --- Stage 2b: ATS JD enrichment for non-JobRight jobs ---
    logger.info("=== Stage 2b: ATS JD Enrichment ===")
    jd_stats = {"processed": 0, "failed": 0, "skipped": 0, "credits_used": 0}
    try:
        with get_connection() as conn:
            jd_stats = run_jd_enrichment(conn=conn)
        logger.info("ATS JD enrichment stats: {}", jd_stats)
    except Exception as e:
        logger.error("ATS JD enrichment crashed: {}", e)
        errors.append(f"ATS JD enrichment crash: {e}")

    # --- Stage 2.5: URL Resolution ---
    logger.info("=== Stage 2.5: URL Resolution ===")
    url_stats = {"simplify": {}, "slug_registry": {}, "serper": {}, "total_resolved": 0, "resolution_rate": 0.0}
    try:
        with get_connection() as conn:
            url_stats = run_url_resolution(conn=conn, batch_size=500)
        logger.info("URL resolution stats: {}", url_stats)
    except Exception as e:
        logger.error("URL resolution crashed: {}", e)
        errors.append(f"URL resolution crash: {e}")

    # --- Stage 2c: LLM fallback for metadata classification ---
    logger.info("=== Stage 2c: LLM Enrichment (metadata classification) ===")
    try:
        enrich_stats = enrich_all()
        logger.info("LLM enrich stats: {}", enrich_stats)
    except Exception as e:
        logger.error("LLM enrichment crashed: {}", e)
        enrich_stats = {"enriched": 0, "failed": 0, "skipped": 0}
        errors.append(f"LLM enrichment crash: {e}")

    # --- Stage 3: Embed ---
    logger.info("=== Stage 3: Embedding ===")
    try:
        embed_stats = embed_all()
        logger.info("Embed stats: {}", embed_stats)
    except Exception as e:
        logger.error("Embedding crashed: {}", e)
        embed_stats = {"embedded": 0, "failed": 0, "skipped": 0}
        errors.append(f"Embedding crash: {e}")

    duration = time.monotonic() - start

    # --- Collect stale job details for the email ---
    stale_jobs: list[dict] = []
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """
                SELECT company_name, role_title, source_repo
                FROM jobs
                WHERE status = 'inactive'
                  AND last_seen_at < NOW() - INTERVAL '1 day'
                  AND last_seen_at > NOW() - INTERVAL '2 days'
                ORDER BY company_name, role_title
                LIMIT 50
                """
            ).fetchall()
            stale_jobs = [dict(r) for r in rows]
    except Exception as e:
        logger.warning("Failed to fetch stale job details: {}", e)

    # --- Notify: complete ---
    send_pipeline_complete_email(
        scrape_stats=scrape_stats,
        jd_stats=jd_stats,
        url_resolution_stats=url_stats,
        enrich_stats=enrich_stats,
        embed_stats=embed_stats,
        duration_seconds=duration,
        errors=errors,
        stale_jobs=stale_jobs,
    )

    return {
        "scrape": scrape_stats,
        "jd_enrichment": jd_stats,
        "url_resolution": url_stats,
        "enrich": enrich_stats,
        "embed": embed_stats,
        "errors": errors,
        "duration_seconds": duration,
    }


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting daily pipeline")
    result = run_daily_pipeline()
    logger.info(
        "Daily pipeline complete. Duration: {:.1f}m",
        result["duration_seconds"] / 60,
    )
    if result["errors"]:
        logger.warning("Errors: {}", result["errors"])
        sys.exit(1)
