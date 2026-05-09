import os
"""Unified daily pipeline orchestrator.

Runs scrape -> enrich -> embed in sequence, captures stats and errors,
and sends email notifications on start and completion.

Standalone CLI usage:
    uv run python -m wekruit_matching.pipeline.daily

Replaces the fragmented daily-update.sh + inline launchd commands.

P7-B (2026-05-08) — Per-stage timeout + always-fire finalizer
=============================================================
Previously the wrapper ``run-pipeline.sh`` enforced a global ``perl alarm 28800``
that SIGKILL'd python when Stage 2c LLM enrichment ran long, bypassing every
``try/except`` and never reaching Stages 3 (embed) + 4 (Firebase sync) + the
completion email. Two consecutive days lost data this way.

Fix:
  1. Each stage runs inside ``_stage_timeout(name, seconds)`` — a context
     manager that arms ``signal.SIGALRM`` for the duration of the stage. If
     the stage exceeds its budget, ``StageTimeoutError`` is raised, captured,
     logged as a warning, and an entry appended to ``errors``. The pipeline
     continues to the next stage.
  2. Stale-job query + completion email + stdout-token print are wrapped in
     a single ``try/finally`` block keyed off ``run_started_at`` — they fire
     no matter what happened upstream.
  3. A new ``pipelineStatus=success|partial|failed`` line is printed last.
     ``run-pipeline.sh`` greps it to set the webhook STATUS, falling back to
     exit-code semantics if the line is missing (forward-compat for partial
     log capture).

Per-stage budgets (subject to tuning):
  Stage 1   Scrape         30 min
  Stage 1.5 Senior + 1.6 Direct APIs   30 min combined (six small scrapers)
  Stage 2a  JobRight free  15 min
  Stage 2b  ATS JD         30 min
  Stage 2c  LLM            90 min  (post P7-A parallelization 10x speedup)
  Stage 3   Embed          30 min
  Stage 4   Firebase sync  20 min

SIGALRM only fires on the main thread (POSIX); P7-A's ThreadPoolExecutor
inside ``enrich_all`` is unaffected — when the stage times out, the main
thread re-raises, the executor's ``as_completed`` loop bubbles up, and
in-flight worker threads finish naturally as the process moves on.
"""
import signal
import sys
import time
from contextlib import contextmanager
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
from wekruit_matching.pipeline.dead_backfill import firestore_dead_backfill
from wekruit_matching.scraper.enrich_from_jobright import enrich_all_jobs as enrich_jobright
from wekruit_matching.scraper.run import scrape_all


# ---------------------------------------------------------------------------
# Per-stage timeout (P7-B)
# ---------------------------------------------------------------------------

class StageTimeoutError(TimeoutError):
    """Raised by the SIGALRM handler when a stage exceeds its time budget."""


@contextmanager
def _stage_timeout(stage_name: str, seconds: int):
    """Arm SIGALRM for the duration of a pipeline stage.

    On expiry, raise ``StageTimeoutError`` so the calling stage's own
    ``try/except`` records it in ``errors`` and the pipeline moves on.

    Caveats:
      * Must run on the main thread (POSIX limits ``signal.signal`` to it).
      * Stage internal threads (e.g. P7-A's enrichment ThreadPoolExecutor)
        are NOT interrupted — only the main-thread waiter (typically the
        ``as_completed`` / ``future.result`` call) is unblocked. Workers
        finish naturally; the process moves on, so they do not stall the
        next stage.
      * ``signal.alarm(0)`` is called in ``finally`` to clear any pending
        alarm before the next stage starts.
    """
    if seconds <= 0:
        # Disabled — caller passed 0 or negative; just yield.
        yield
        return

    def _handler(signum, frame):
        raise StageTimeoutError(
            f"Stage '{stage_name}' exceeded {seconds}s budget"
        )

    prev_handler = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev_handler)


# Stage budgets (seconds). Centralised so tests can monkeypatch.
STAGE_BUDGETS = {
    "dead_backfill": 5 * 60,  # P7-K Stage 0 — Firestore dead-flag mirror
    "scrape": 30 * 60,
    "senior_scrapers": 30 * 60,  # combined Stage 1.5 + 1.6
    "jobright": 15 * 60,
    "jd_enrich": 30 * 60,
    "llm_enrich": 90 * 60,
    "embed": 30 * 60,
    "sync": 20 * 60,
}


def _record_timeout(errors: list[str], stage: str, exc: StageTimeoutError) -> None:
    """Uniform timeout-error reporting."""
    msg = f"{stage} TIMEOUT: {exc}"
    logger.warning(msg)
    errors.append(msg)


def run_daily_pipeline() -> dict:
    """Execute the full daily pipeline with email notifications.

    Returns a dict with all stage stats and any errors encountered.
    Always sends completion email + emits stdout tokens, even if upstream
    stages raised exceptions or timed out.
    """
    run_started_at = datetime.now(UTC)
    start = time.monotonic()
    errors: list[str] = []

    # Default empty stats so finalizer always has something to email.
    scrape_stats: dict = {}
    jobright_stats: dict = {"enriched": 0, "failed": 0, "skills_found": 0}
    jd_stats: dict = {"processed": 0, "failed": 0, "skipped": 0, "credits_used": 0}
    enrich_stats: dict = {"enriched": 0, "failed": 0, "skipped": 0}
    embed_stats: dict = {"embedded": 0, "failed": 0, "skipped": 0}
    sync_stats: dict = {"active_jobs": 0, "inactive_jobs": 0, "synced": 0, "batches": 0}
    dead_backfill_stats: dict = {"synced": 0, "total_seen": 0, "skipped": ""}
    stage_outcomes: dict[str, str] = {}  # stage_name -> "ok"|"error"|"timeout"

    # --- Notify: start ---
    send_pipeline_start_email()

    try:
        # --- Stage 0: Firestore dead-flag backfill (P7-K) ---
        # Mirror Firestore matching-jobs.dead==true into Postgres jobs.dead
        # so the scraper UPSERT below can short-circuit on already-dead URLs.
        # Graceful skip if creds aren't configured — logs warning, continues.
        logger.info("=== Stage 0: Firestore Dead-Flag Backfill ===")
        try:
            with _stage_timeout("dead_backfill", STAGE_BUDGETS["dead_backfill"]):
                with get_connection() as conn:
                    dead_backfill_stats = firestore_dead_backfill(conn)
                logger.info("Dead-backfill stats: {}", dead_backfill_stats)
                stage_outcomes["dead_backfill"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "dead_backfill", e)
            stage_outcomes["dead_backfill"] = "timeout"
        except Exception as e:
            logger.error("Dead-backfill crashed: {}", e)
            errors.append(f"Dead-backfill crash: {e}")
            stage_outcomes["dead_backfill"] = "error"

        # --- Stage 1: Scrape ---
        logger.info("=== Stage 1: Scraping ===")
        try:
            with _stage_timeout("scrape", STAGE_BUDGETS["scrape"]):
                scrape_stats = scrape_all()
                logger.info("Scrape stats: {}", scrape_stats)
                for source, stats in scrape_stats.items():
                    if "error" in stats:
                        errors.append(f"Scrape {source}: {stats['error']}")
                stage_outcomes["scrape"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "scrape", e)
            scrape_stats = {"pipeline": {"error": "timeout"}}
            stage_outcomes["scrape"] = "timeout"
        except Exception as e:
            logger.error("Scraper crashed: {}", e)
            scrape_stats = {"pipeline": {"error": str(e)}}
            errors.append(f"Scraper crash: {e}")
            stage_outcomes["scrape"] = "error"

        # --- Stage 1.5 + 1.6: Multi-source senior + direct-API scrapers ---
        # LinkedIn / Wellfound / Otta / Greenhouse / Lever / Ashby — gated by
        # ENABLE_<SRC>_SCRAPE env flags. Each scraper is independently
        # toggleable so partial outages don't block the rest of the pipeline.
        # Output is collected, deduped against the in-memory pool with
        # dedup_multi_source(), then upserted with sources=[...] preserved.
        logger.info("=== Stage 1.5+1.6: Multi-source senior + direct-API scrapers ===")
        senior_stats: dict[str, dict] = {}
        senior_jobs: list = []
        try:
            with _stage_timeout("senior_scrapers", STAGE_BUDGETS["senior_scrapers"]):
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

                # Stage 1.6 — Phase 73 career-ops port: direct public-API scrapers
                if os.environ.get("ENABLE_GREENHOUSE_DIRECT", "1") == "1":
                    try:
                        from wekruit_matching.scraper.greenhouse_direct import (
                            scrape_greenhouse_direct,
                        )
                        gh_jobs = scrape_greenhouse_direct()
                        senior_jobs.extend(gh_jobs)
                        senior_stats["greenhouse_direct"] = {"scraped": len(gh_jobs)}
                        logger.info("greenhouse_direct scraped {} jobs", len(gh_jobs))
                    except Exception as e:
                        logger.warning("greenhouse_direct scrape failed: {}", e)
                        senior_stats["greenhouse_direct"] = {"error": str(e)}
                        errors.append(f"Greenhouse direct: {e}")

                if os.environ.get("ENABLE_LEVER_DIRECT", "1") == "1":
                    try:
                        from wekruit_matching.scraper.lever_direct import scrape_lever_direct
                        lv_jobs = scrape_lever_direct()
                        senior_jobs.extend(lv_jobs)
                        senior_stats["lever_direct"] = {"scraped": len(lv_jobs)}
                        logger.info("lever_direct scraped {} jobs", len(lv_jobs))
                    except Exception as e:
                        logger.warning("lever_direct scrape failed: {}", e)
                        senior_stats["lever_direct"] = {"error": str(e)}
                        errors.append(f"Lever direct: {e}")

                if os.environ.get("ENABLE_ASHBY_DIRECT", "1") == "1":
                    try:
                        from wekruit_matching.scraper.ashby_direct import scrape_ashby_direct
                        ab_jobs = scrape_ashby_direct()
                        senior_jobs.extend(ab_jobs)
                        senior_stats["ashby_direct"] = {"scraped": len(ab_jobs)}
                        logger.info("ashby_direct scraped {} jobs", len(ab_jobs))
                    except Exception as e:
                        logger.warning("ashby_direct scrape failed: {}", e)
                        senior_stats["ashby_direct"] = {"error": str(e)}
                        errors.append(f"Ashby direct: {e}")

                if senior_jobs:
                    try:
                        from wekruit_matching.scraper.dedup import dedup_multi_source
                        from wekruit_matching.scraper.upsert import (
                            mark_stale_jobs as _mark_stale,
                            upsert_jobs as _upsert,
                        )

                        deduped = dedup_multi_source(senior_jobs)
                        logger.info(
                            "Stage 1.5 dedup: {} -> {} after multi-source collapse",
                            len(senior_jobs), len(deduped),
                        )
                        with get_connection() as conn:
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
                stage_outcomes["senior_scrapers"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "senior_scrapers", e)
            stage_outcomes["senior_scrapers"] = "timeout"

        # Merge senior_stats into scrape_stats so downstream email + webhook
        # token totals see them.
        for k, v in senior_stats.items():
            scrape_stats.setdefault(k, v)

        # --- Stage 2a: Enrich from JobRight pages (FREE — no LLM) ---
        logger.info("=== Stage 2a: JobRight Page Enrichment (free) ===")
        try:
            with _stage_timeout("jobright", STAGE_BUDGETS["jobright"]):
                with get_connection() as conn:
                    jobright_stats = enrich_jobright(conn, max_workers=8, batch_size=50)
                logger.info("JobRight enrich stats: {}", jobright_stats)
                stage_outcomes["jobright"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "jobright", e)
            stage_outcomes["jobright"] = "timeout"
        except Exception as e:
            logger.error("JobRight enrichment crashed: {}", e)
            errors.append(f"JobRight enrichment crash: {e}")
            stage_outcomes["jobright"] = "error"

        # --- Stage 2b: ATS JD enrichment for non-JobRight jobs ---
        logger.info("=== Stage 2b: ATS JD Enrichment ===")
        try:
            with _stage_timeout("jd_enrich", STAGE_BUDGETS["jd_enrich"]):
                with get_connection() as conn:
                    jd_stats = run_jd_enrichment(conn=conn)
                logger.info("ATS JD enrichment stats: {}", jd_stats)
                stage_outcomes["jd_enrich"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "jd_enrich", e)
            stage_outcomes["jd_enrich"] = "timeout"
        except Exception as e:
            logger.error("ATS JD enrichment crashed: {}", e)
            errors.append(f"ATS JD enrichment crash: {e}")
            stage_outcomes["jd_enrich"] = "error"

        # --- Stage 2.5 deleted (Phase 66, 2026-05-06) ---
        # URL resolution migrated to wekruit-pa Cloud Function.

        # --- Stage 2c: LLM fallback for metadata classification ---
        logger.info("=== Stage 2c: LLM Enrichment (metadata classification) ===")
        try:
            with _stage_timeout("llm_enrich", STAGE_BUDGETS["llm_enrich"]):
                enrich_stats = enrich_all()
                logger.info("LLM enrich stats: {}", enrich_stats)
                stage_outcomes["llm_enrich"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "llm_enrich", e)
            stage_outcomes["llm_enrich"] = "timeout"
        except Exception as e:
            logger.error("LLM enrichment crashed: {}", e)
            errors.append(f"LLM enrichment crash: {e}")
            stage_outcomes["llm_enrich"] = "error"

        # --- Stage 3: Embed ---
        logger.info("=== Stage 3: Embedding ===")
        try:
            with _stage_timeout("embed", STAGE_BUDGETS["embed"]):
                embed_stats = embed_all()
                logger.info("Embed stats: {}", embed_stats)
                stage_outcomes["embed"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "embed", e)
            stage_outcomes["embed"] = "timeout"
        except Exception as e:
            logger.error("Embedding crashed: {}", e)
            errors.append(f"Embedding crash: {e}")
            stage_outcomes["embed"] = "error"

        # --- Stage 4: Sync active/inactive jobs to Firebase ---
        logger.info("=== Stage 4: Firebase Job Sync ===")
        try:
            with _stage_timeout("sync", STAGE_BUDGETS["sync"]):
                sync_stats = sync_jobs_to_firebase(since=run_started_at, full_sync=False)
                logger.info("Firebase sync stats: {}", sync_stats)
                stage_outcomes["sync"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "sync", e)
            stage_outcomes["sync"] = "timeout"
        except Exception as e:
            logger.error("Job sync crashed: {}", e)
            errors.append(f"Job sync crash: {e}")
            stage_outcomes["sync"] = "error"

    finally:
        # =========================================================
        # Always-fire finalizer (P7-B)
        # =========================================================
        # Runs even if an unhandled exception escapes the try block.
        # All stage stats default to empty dicts so the email + webhook
        # tokens always have safe values to read.
        # =========================================================
        duration = time.monotonic() - start

        # Collect stale job details for the email
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

        # Notify: complete (always — even on partial / total failure)
        try:
            send_pipeline_complete_email(
                scrape_stats=scrape_stats,
                jd_stats=jd_stats,
                enrich_stats=enrich_stats,
                embed_stats=embed_stats,
                duration_seconds=duration,
                errors=errors,
                stale_jobs=stale_jobs,
            )
        except Exception as e:
            logger.warning("send_pipeline_complete_email failed: {}", e)

        # Determine pipeline status for the wrapper webhook.
        # We weight by CORE stages only (scrape, jd_enrich, llm_enrich,
        # embed, sync). Auxiliary stages (senior_scrapers, jobright) are
        # bonus enrichment — they do not gate "success" or "failed":
        #   * core_ok > 0 + errors empty => success
        #   * core_ok > 0 + errors present => partial
        #   * core_ok == 0 (every core stage errored/timed-out) => failed
        # Rationale: a run where every core stage crashed is a real failure
        # even if auxiliary scrapers happened to no-op cleanly. A run where
        # some core stages succeeded plus some auxiliary errors is "partial".
        CORE_STAGES = {"scrape", "jd_enrich", "llm_enrich", "embed", "sync"}
        core_ok = sum(
            1 for stage, v in stage_outcomes.items()
            if stage in CORE_STAGES and v == "ok"
        )
        if not errors and core_ok > 0:
            pipeline_status = "success"
        elif core_ok == 0:
            pipeline_status = "failed"
        else:
            pipeline_status = "partial"

        # v1.5 Stream-A2 — emit normalized stat tokens for the bash webhook.
        # Token names must match the grep -oE patterns in run-pipeline.sh.
        # scrape_stats shape: {repo_slug: {inserted, updated, unchanged, stale}}
        # or {repo_slug: {error}}.
        try:
            print(f"jobsScraped={sum(s.get('inserted',0)+s.get('updated',0)+s.get('unchanged',0) for s in scrape_stats.values() if 'error' not in s)}")
            print(f"jobsNew={sum(s.get('inserted',0) for s in scrape_stats.values() if 'error' not in s)}")
            print(f"jobsUpdated={sum(s.get('updated',0) for s in scrape_stats.values() if 'error' not in s)}")
            print(f"jobsErrored={len(errors)}")
            print(f"costUsd=0")  # no cost_usd field plumbed yet
            # NEW (P7-B) — wrapper greps this to override exit-code-based status
            print(f"pipelineStatus={pipeline_status}")
            # Stage outcomes for diagnostic purposes (parsed by future tooling)
            for stage, outcome in stage_outcomes.items():
                print(f"stageOutcome.{stage}={outcome}")
        except Exception as e:
            logger.warning("Failed to emit stdout stat tokens: {}", e)

    return {
        "dead_backfill": dead_backfill_stats,
        "scrape": scrape_stats,
        "jd_enrichment": jd_stats,
        "enrich": enrich_stats,
        "embed": embed_stats,
        "sync": sync_stats,
        "errors": errors,
        "duration_seconds": duration,
        "pipeline_status": pipeline_status,
        "stage_outcomes": stage_outcomes,
    }


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting daily pipeline")
    result = run_daily_pipeline()
    logger.info(
        "Daily pipeline complete. Duration: {:.1f}m status={}",
        result["duration_seconds"] / 60,
        result.get("pipeline_status", "unknown"),
    )
    if result["errors"]:
        logger.warning("Errors: {}", result["errors"])
        # Exit non-zero on partial OR failed so launchd surfaces it; the
        # wrapper still grabs the precise status from stdout token.
        sys.exit(1)
