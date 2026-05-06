import os
"""Unified daily pipeline orchestrator.

Runs scrape -> enrich -> embed in sequence, captures stats and errors,
and sends email notifications on start and completion.

Standalone CLI usage:
    uv run python -m wekruit_matching.pipeline.daily

Replaces the fragmented daily-update.sh + inline launchd commands.
"""
import sys
import time
from datetime import UTC, datetime

from loguru import logger

from wekruit_matching.db.connection import get_connection
from wekruit_matching.embedding.run import embed_all
from wekruit_matching.enrichment.run import enrich_all
from wekruit_matching.notifications.email import (
    send_pipeline_complete_email,
    send_pipeline_start_email,
)
from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase
from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment
from wekruit_matching.pipeline.run_url_resolution import run_url_resolution
from wekruit_matching.scraper.enrich_from_jobright import enrich_all_jobs as enrich_jobright
from wekruit_matching.scraper.run import scrape_all


def run_daily_pipeline() -> dict:
    """Execute the full daily pipeline with email notifications.

    Returns a dict with all stage stats and any errors encountered.
    """
    run_started_at = datetime.now(UTC)
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

    # --- Stage 1.5: Multi-source senior scrapers (Phase 63 v1.7) ---
    # LinkedIn / Wellfound / Otta — gated by ENABLE_<SRC>_SCRAPE env flags
    # in /Users/Shared/wekruit/.env-secrets. Each scraper is independently
    # toggleable so partial outages don't block the rest of the pipeline.
    # Output is collected, deduped against the in-memory pool with
    # dedup_multi_source(), then upserted with sources=[...] preserved on
    # each Job. The Firebase sync layer reads `sources` and writes
    # matching-jobs.{id}.sources arrays.
    logger.info("=== Stage 1.5: Multi-source senior scrapers (Phase 63) ===")
    senior_stats: dict[str, dict] = {}
    senior_jobs: list = []

    if os.environ.get("ENABLE_WELLFOUND_SCRAPE", "1") == "1":
        try:
            from wekruit_matching.scraper.wellfound import scrape_wellfound
            wf_jobs = scrape_wellfound()
            senior_jobs.extend(wf_jobs)
            senior_stats["wellfound"] = {"scraped": len(wf_jobs)}
            logger.info("wellfound scraped {} jobs", len(wf_jobs))
        except Exception as e:
            logger.warning("wellfound scrape failed: {}", e)
            senior_stats["wellfound"] = {"error": str(e)}
            errors.append(f"Wellfound scrape: {e}")

    if os.environ.get("ENABLE_LINKEDIN_SCRAPE", "0") == "1":
        try:
            from wekruit_matching.scraper.linkedin import scrape_linkedin
            li_jobs = scrape_linkedin()
            senior_jobs.extend(li_jobs)
            senior_stats["linkedin"] = {"scraped": len(li_jobs)}
            logger.info("linkedin scraped {} jobs", len(li_jobs))
        except Exception as e:
            logger.warning("linkedin scrape failed: {}", e)
            senior_stats["linkedin"] = {"error": str(e)}
            errors.append(f"LinkedIn scrape: {e}")

    if os.environ.get("ENABLE_OTTA_SCRAPE", "0") == "1":
        try:
            from wekruit_matching.scraper.otta import scrape_otta
            ot_jobs = scrape_otta()
            senior_jobs.extend(ot_jobs)
            senior_stats["otta"] = {"scraped": len(ot_jobs)}
            logger.info("otta scraped {} jobs", len(ot_jobs))
        except Exception as e:
            logger.warning("otta scrape failed: {}", e)
            senior_stats["otta"] = {"error": str(e)}
            errors.append(f"Otta scrape: {e}")

    if senior_jobs:
        try:
            from wekruit_matching.scraper.dedup import dedup_multi_source
            from wekruit_matching.scraper.upsert import (
                mark_stale_jobs as _mark_stale,
                upsert_jobs as _upsert,
            )

            deduped = dedup_multi_source(senior_jobs)
            logger.info(
                "Stage 1.5 dedup: {} → {} after multi-source collapse",
                len(senior_jobs), len(deduped),
            )
            with get_connection() as conn:
                # Group by source_repo for upsert + stale-mark scoping.
                by_repo: dict[str, list] = {}
                for j in deduped:
                    by_repo.setdefault(j.source_repo, []).append(j)
                for repo_slug, group in by_repo.items():
                    upsert_stats = _upsert(group, conn)
                    seen_ids = {j.job_id for j in group}
                    stale_count = _mark_stale(seen_ids, repo_slug, conn)
                    senior_stats[repo_slug] = {
                        **(senior_stats.get(repo_slug) or {}),
                        **upsert_stats,
                        "stale": stale_count,
                    }
        except Exception as e:
            logger.error("Stage 1.5 upsert crashed: {}", e)
            errors.append(f"Stage 1.5 upsert: {e}")

    # Merge senior_stats into scrape_stats so downstream email + webhook
    # token totals see them.
    for k, v in senior_stats.items():
        scrape_stats.setdefault(k, v)

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
    # iter34 hotfix 2026-05-05 — Stage 2.5 hangs on Supabase pooler poll().
    # Adding multiprocess-level timeout so a stuck stage cannot block
    # Stage 3/4. Honor SKIP_URL_RESOLUTION env to skip entirely.
    logger.info("=== Stage 2.5: URL Resolution ===")
    url_stats = {
        "simplify": {},
        "slug_registry": {},
        "serper": {},
        "total_resolved": 0,
        "resolution_rate": 0.0,
    }
    if os.environ.get("SKIP_URL_RESOLUTION", "").lower() in ("1", "true", "yes"):
        logger.warning("Stage 2.5 skipped via SKIP_URL_RESOLUTION env")
    else:
        import threading
        result_holder: dict = {}
        def _runner():
            try:
                with get_connection() as conn:
                    result_holder["stats"] = run_url_resolution(conn=conn, batch_size=500)
            except Exception as e:
                result_holder["error"] = e
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=600)  # 10 min hard cap
        if t.is_alive():
            logger.error("URL resolution timed out after 10min — proceeding to Stage 3")
            errors.append("URL resolution timeout (Supabase pooler hang)")
        elif "error" in result_holder:
            logger.error("URL resolution crashed: {}", result_holder["error"])
            errors.append(f"URL resolution crash: {result_holder['error']}")
        else:
            url_stats = result_holder.get("stats", url_stats)
            logger.info("URL resolution stats: {}", url_stats)

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

    # --- Stage 4: Sync active/inactive jobs to Firebase ---
    logger.info("=== Stage 4: Firebase Job Sync ===")
    try:
        sync_stats = sync_jobs_to_firebase(since=run_started_at, full_sync=False)
        logger.info("Firebase sync stats: {}", sync_stats)
    except Exception as e:
        logger.error("Job sync crashed: {}", e)
        sync_stats = {"active_jobs": 0, "inactive_jobs": 0, "synced": 0, "batches": 0}
        errors.append(f"Job sync crash: {e}")

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

    # v1.5 Stream-A2 — emit normalized stat tokens for the bash webhook in
    # scripts/post-pipeline-webhook.sh. Token names must match the grep -oE
    # patterns in scripts/daily-update.sh. scrape_stats shape:
    # {repo_slug: {inserted, updated, unchanged, stale}} or {repo_slug: {error}}.
    print(f"jobsScraped={sum(s.get('inserted',0)+s.get('updated',0)+s.get('unchanged',0) for s in scrape_stats.values() if 'error' not in s)}")
    print(f"jobsNew={sum(s.get('inserted',0) for s in scrape_stats.values() if 'error' not in s)}")
    print(f"jobsUpdated={sum(s.get('updated',0) for s in scrape_stats.values() if 'error' not in s)}")
    print(f"jobsErrored={len(errors)}")
    print(f"costUsd=0")  # no cost_usd field exists in any stats dict — plumb separately

    return {
        "scrape": scrape_stats,
        "jd_enrichment": jd_stats,
        "url_resolution": url_stats,
        "enrich": enrich_stats,
        "embed": embed_stats,
        "sync": sync_stats,
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
