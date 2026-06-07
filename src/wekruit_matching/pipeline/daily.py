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
from wekruit_matching.db.schema_guard import ensure_schema_current
from wekruit_matching.embedding.run import embed_all
from wekruit_matching.enrichment.run import enrich_all
from wekruit_matching.notifications.email import (
    send_dependency_alert,
    send_pipeline_complete_email,
    send_pipeline_start_email,
)
from wekruit_matching.pipeline import preflight as preflight_mod
from wekruit_matching.pipeline.health_gate import (
    PreSyncGateError,
    assert_pre_sync_ready,
    run_health_gate,
)
from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase
from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment
from wekruit_matching.pipeline.dead_backfill import (
    firestore_dead_backfill,
    firestore_reconcile,
    reconcile_dead_inactive,
)
from wekruit_matching.scraper.enrich_from_jobright import enrich_all_jobs as enrich_jobright
from wekruit_matching.scraper.run import scrape_all

# Stage 2.5 — ATS resolve (re-introduced 2026-05-30, fix #2). The resolver lives
# in scripts/ (a top-level package, scripts/__init__.py present) and the daily
# run executes from the repo root (`daily-update.sh` cd's there before
# `python -m wekruit_matching.pipeline.daily`). Guard the import so a path hiccup
# degrades Stage 2.5 to a skip rather than crashing the whole module load. The
# stage below checks `resolve_jobright_pending is not None`; tests monkeypatch
# this module attribute directly.
try:
    from scripts.resolve_jobright_ats import resolve_jobright_pending
except Exception as _ats_import_err:  # pragma: no cover - import-path safety
    resolve_jobright_pending = None
    logger.warning(
        "Stage 2.5 ATS resolve import unavailable ({}); stage will skip",
        _ats_import_err,
    )
# Stage 1.7 — VC portfolio job boards via self-hosted Firecrawl.
# See `.planning/INITIATIVE-vc-portfolio-job-boards.md` for the 17-board roster.
from wekruit_matching.scraper.vc_board import (
    FirecrawlClient as VCFirecrawlClient,
    scrape_all_boards as scrape_all_vc_boards,
)


# ---------------------------------------------------------------------------
# Per-stage timeout (P7-B)
# ---------------------------------------------------------------------------

class StageTimeoutError(TimeoutError):
    """Raised by the SIGALRM handler when a stage exceeds its time budget."""


class _PreflightAbort(Exception):
    """Internal control-flow signal: Stage 0 preflight hard-failed (a required
    core dependency is missing). Raised inside the main try so the always-fire
    finalizer (completion email + ``pipelineStatus`` sentinel) still runs, then
    caught just before ``finally`` so the run ends as 'failed' with no stages
    executed. Never escapes ``run_daily_pipeline``."""


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
    "dead_backfill": 10 * 60,  # P7-K Stage 0 — Firestore dead-flag mirror
    "scrape": 45 * 60,
    "senior_scrapers": 45 * 60,  # combined Stage 1.5 + 1.6
    # 2026-05-27 — Stage 1.7: 16 VC portfolio boards via Firecrawl render.
    # Each board is one POST /v1/scrape (5-8s render). 16 boards × ~10s
    # serial = ~3min normal. 20-min budget covers Firecrawl pool warmups
    # plus a stuck render or two without bleeding into Stage 2.
    "vc_boards": 20 * 60,
    "jobright": 20 * 60,
    "jd_enrich": 90 * 60,        # bumped 30→90 — Firecrawl JS pages slow
    "llm_enrich": 120 * 60,      # bumped 90→120 — gap-fill backlog
    # Stage 2.5 (2026-05-30) — re-introduced ATS resolve via Serper. Each
    # pending jobright row is up to 2 Serper POSTs + a HEAD liveness check,
    # parallelized across a thread pool. 30 min covers a normal pending backlog
    # without bleeding into embed/sync. Gated on SERPER_API_KEY being set.
    "ats_resolve": 30 * 60,
    "embed": 45 * 60,
    "sync": 30 * 60,
    # Stage 4.5 (2026-05-30) — Firestore<->Postgres reconcile; one PG COUNT(*)
    # plus two Firestore aggregation COUNTs. 5 min is generous headroom.
    "firestore_reconcile": 5 * 60,
    # P-REL Stage 5 — read-only data-quality gate; a handful of COUNT(*)
    # queries against the live DB. 5 min is generous headroom.
    "health_gate": 5 * 60,
}


def _record_timeout(errors: list[str], stage: str, exc: StageTimeoutError) -> None:
    """Uniform timeout-error reporting."""
    msg = f"{stage} TIMEOUT: {exc}"
    logger.warning(msg)
    errors.append(msg)


def _flag_degraded(
    stage: str,
    reason: str,
    *,
    stage_outcomes: dict[str, str],
    errors: list[str],
) -> None:
    """Mark a stage DEGRADED and record a human-visible reason.

    A "degraded" stage ran without raising but produced a DEGENERATE result
    (resolved=0, embedded=0, every row failed, a skip-sentinel, terminally
    rejected docs). Before 2026-06-04 every stage stamped ``"ok"`` purely on
    "the call returned without raising", so a dependency that came back EMPTY
    (a dead Serper, a no-cred Firestore mirror, an all-failed JD batch) reported
    success with zero alerts — that is how a credit-exhausted Serper went unseen
    for days.

    Recording the reason in ``errors`` is deliberate: it (1) flips
    pipeline_status to ``"partial"`` and (2) surfaces in the completion email +
    the [DEGRADED] subject prefix — i.e. it reaches a human. ``stage_outcomes``
    keeps the per-stage ``"degraded"`` marker for the webhook/diagnostics.
    """
    logger.error("stage degraded: {} -- {}", stage, reason)
    errors.append(f"{stage} degraded: {reason}")
    stage_outcomes[stage] = "degraded"


def run_daily_pipeline() -> dict:
    """Execute the full daily pipeline with email notifications.

    Returns a dict with all stage stats and any errors encountered.
    Always sends completion email + emits stdout tokens, even if upstream
    stages raised exceptions or timed out.
    """
    run_started_at = datetime.now(UTC)
    start = time.monotonic()
    # Pipeline-level errors — only catastrophic stage crashes/timeouts go
    # here. Per-source dependency failures (a single Firecrawl/Serper/source
    # 5xx, one Postgres statement timeout on an auxiliary scraper) go to
    # `dependency_errors` and never gate pipeline_status. Treating external
    # dependencies as always-working would mean ignoring them; what we
    # actually want is treating them as best-effort: log + move on, never
    # propagate to "partial" status. The status is only "partial" when a
    # CORE stage itself crashed or timed out, not when a sub-component of
    # a stage had a transient hiccup.
    errors: list[str] = []
    dependency_errors: list[str] = []

    # Default empty stats so finalizer always has something to email.
    scrape_stats: dict = {}
    jobright_stats: dict = {"enriched": 0, "failed": 0, "skills_found": 0}
    jd_stats: dict = {"processed": 0, "failed": 0, "skipped": 0, "credits_used": 0}
    enrich_stats: dict = {"enriched": 0, "failed": 0, "skipped": 0}
    embed_stats: dict = {"embedded": 0, "failed": 0, "skipped": 0}
    sync_stats: dict = {"active_jobs": 0, "inactive_jobs": 0, "synced": 0, "batches": 0}
    # Stage 2.5 ATS resolve stats — hoisted so the finalizer can wire the
    # resolver's resolved/missed/infra_error into the completion email
    # (url_resolution_stats) even when the stage is skipped.
    ats_stats: dict = {"resolved": 0, "missed": 0, "infra_error": 0, "infra_detail": ""}
    dead_backfill_stats: dict = {"synced": 0, "total_seen": 0, "skipped": ""}
    stage_outcomes: dict[str, str] = {}  # stage_name -> "ok"|"error"|"timeout"
    # Post-run reliability gate (P-REL): data-quality failures discovered by
    # querying the live DB after the run. Populated by run_health_gate() as the
    # final stage; surfaced in the completion email + folded into errors so a
    # coverage/matchable-corpus regression is caught by the pipeline, not users.
    health_metrics: dict = {}
    health_failures: list[dict] = []
    # WS-B Gate-6: when True, the Firestore sync stage is skipped this run.
    # Set by Stage 0 preflight (sync credential down OR WEKRUIT_SKIP_SYNC=1) or
    # by the Stage 3.5 blocking pre-sync gate (corrupt batch must not reach
    # Firestore). The rest of the pipeline still runs so Postgres stays current.
    skip_sync = False

    # --- WS-B CID-05: startup schema-current guard (fail fast) ---
    # Catch ANY entrypoint that ran the pipeline without first migrating: if the
    # DB schema is OLDER than the code expects (alembic current != head), abort
    # the run loudly rather than operate on a skewed schema. daily-update.sh
    # already runs `alembic upgrade head` + a head-assert before us; this is the
    # belt-and-suspenders for direct/forgotten invocations. ensure_schema_current
    # fails OPEN on an undeterminable revision (does not wedge), CLOSED on a real
    # skew. Runs BEFORE the start email + any stage so a skewed run does no work.
    # Use the module-level get_connection (so tests can patch it) and pass the
    # open connection in, rather than letting schema_guard open its own — keeps
    # this offline/patchable and reuses the pool connection.
    try:
        with get_connection() as conn:
            ensure_schema_current(conn)
    except Exception as e:  # noqa: BLE001 - genuine skew (RuntimeError) or worse
        logger.error("Schema guard FAILED (aborting run): {}", e)
        errors.append(f"Schema guard: {e}")
        stage_outcomes["schema_guard"] = "error"
        # Emit the sentinel + diagnostic tokens that daily-scrape.yml and
        # daily-update.sh grep, then abort. No stage ran, so status is 'failed'.
        try:
            print("jobsScraped=0")
            print("jobsNew=0")
            print("jobsUpdated=0")
            print(f"jobsErrored={len(errors)}")
            print("costUsd=0")
            print("pipelineStatus=failed")
            for stage, outcome in stage_outcomes.items():
                print(f"stageOutcome.{stage}={outcome}")
        except Exception as _tok_err:  # noqa: BLE001
            logger.warning("Failed to emit schema-guard abort tokens: {}", _tok_err)
        return {
            "scrape": {},
            "errors": errors,
            "dependency_errors": dependency_errors,
            "duration_seconds": time.monotonic() - start,
            "pipeline_status": "failed",
            "stage_outcomes": stage_outcomes,
            "health_metrics": health_metrics,
            "health_failures": health_failures,
        }

    # --- Notify: start ---
    send_pipeline_start_email()

    try:
        # --- WS-B Stage 0: dependency / credential preflight ---
        # Decide UP FRONT whether the night can sync. A missing core secret
        # (DATABASE_URL / ANTHROPIC / OPENAI) is a HARD FAIL -> abort. A
        # Firestore credential that is configured-but-unusable (expired/revoked
        # SA) is a SOFT DEGRADE -> skip ONLY sync and keep scrape/enrich/embed so
        # Postgres stays current and the next healthy night syncs the backlog.
        # WEKRUIT_SKIP_SYNC=1 (set by daily-update.sh when preflight exited 2)
        # forces the same degrade. preflight does NO network at import and only
        # mints a live token when WEKRUIT_PREFLIGHT_PROBE_FIRESTORE is set.
        logger.info("=== Stage 0: Dependency / Credential Preflight ===")
        try:
            pf = preflight_mod.run_preflight()
            if pf.problems:
                for _p in pf.problems:
                    logger.warning("preflight: {}", _p)
            if pf.hard_fail:
                logger.error(
                    "Preflight HARD FAIL (aborting run): {}",
                    "; ".join(pf.problems) or "missing required core dependency",
                )
                errors.append(
                    "Preflight hard fail: "
                    + ("; ".join(pf.problems) or "missing required core dependency")
                )
                stage_outcomes["preflight"] = "error"
                raise _PreflightAbort()
            if (not pf.sync_ok) or os.environ.get("WEKRUIT_SKIP_SYNC") == "1":
                skip_sync = True
                reason = (
                    "WEKRUIT_SKIP_SYNC=1"
                    if os.environ.get("WEKRUIT_SKIP_SYNC") == "1"
                    else "Firestore/sync credential down"
                )
                logger.warning(
                    "degrade: sync will be skipped this run ({})", reason
                )
                stage_outcomes["preflight"] = "degraded"
            else:
                stage_outcomes["preflight"] = "ok"
        except _PreflightAbort:
            raise
        except Exception as e:  # noqa: BLE001 - preflight itself failing is non-fatal
            # The preflight is best-effort guidance; if IT crashes we proceed
            # rather than no-op the whole night. Record it for visibility.
            logger.warning("Preflight crashed (non-fatal, proceeding): {}", e)
            stage_outcomes["preflight"] = "error"

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
                # The dead mirror is what flips confirmed-dead postings to
                # inactive (Stage 3.5). If it SKIPPED (no SDK / no creds), nothing
                # gets reconciled and dead jobs ride into the live matcher as
                # clickable 404s — the documented "dead jobs served to users"
                # regression, silently re-armed by a Firestore credential outage.
                # Do NOT stamp 'ok' on a skip: degrade + alert a human.
                _skip = dead_backfill_stats.get("skipped")
                if _skip:
                    _flag_degraded(
                        "dead_backfill",
                        f"Firestore dead-flag mirror skipped ({_skip}) — "
                        "dead jobs will NOT be reconciled this run and may stay "
                        "active in the matcher",
                        stage_outcomes=stage_outcomes,
                        errors=errors,
                    )
                    send_dependency_alert(
                        "Firestore (dead-flag mirror)",
                        f"dead-backfill skipped: {_skip}",
                        impact=(
                            "Confirmed-dead postings are not being flipped "
                            "inactive — they may be served to users as 404 links."
                        ),
                        action="Restore Firestore SDK / credentials.",
                    )
                else:
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
                        # Per-source scrape failures (jobright statement
                        # timeout, yc transaction abort, etc) are best-effort:
                        # we log + record under dependency_errors but do NOT
                        # flip the run to "partial". Only an outright Stage 1
                        # crash/timeout fails the stage.
                        msg = f"Scrape {source}: {stats['error']}"
                        logger.warning("dependency-degraded: {}", msg)
                        dependency_errors.append(msg)
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
                        dependency_errors.append(f"Wellfound scrape: {e}")

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
                        dependency_errors.append(f"LinkedIn scrape: {e}")

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
                        dependency_errors.append(f"Otta scrape: {e}")

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
                        dependency_errors.append(f"Greenhouse direct: {e}")

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
                        dependency_errors.append(f"Lever direct: {e}")

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
                        dependency_errors.append(f"Ashby direct: {e}")

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
                            from wekruit_matching.scraper.upsert import (
                                STALE_GUARD_TRIPPED,
                            )
                            for repo_slug, group in by_repo.items():
                                upsert_stats = _upsert(group, conn)
                                seen_ids = {j.job_id for j in group}
                                stale_count = _mark_stale(seen_ids, repo_slug, conn)
                                if stale_count == STALE_GUARD_TRIPPED:
                                    # Partial-scrape circuit-breaker tripped: the
                                    # run would have mass-deactivated live jobs.
                                    # Surface it so the health gate / email flags
                                    # a degraded source instead of silently
                                    # losing the corpus.
                                    errors.append(
                                        f"Stage 1.5 stale-guard tripped for "
                                        f"{repo_slug}: partial scrape, skipped "
                                        f"deactivation"
                                    )
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

        # --- Stage 1.7: VC portfolio job boards via self-hosted Firecrawl ---
        # 2026-05-27 — Adam ask: scrape 17 VC boards (a16z, Sequoia, Accel,
        # Khosla, KP, Greylock, NEA, Lightspeed, Bessemer, Battery, GC, Index,
        # Contrary, Pear, Antler, BITKRAFT) via render+regex, not by
        # reverse-engineering Consider/Getro/Ashby SaaS APIs. One adapter,
        # 16 config rows. See `scraper/vc_board.py`.
        #
        # Skipped silently when FIRECRAWL_BASE_URL is unset OR points at
        # cloud Firecrawl with no key (config.py treats blank as opt-out
        # for downstream Stage 2b, mirror that behaviour here).
        logger.info("=== Stage 1.7: VC Portfolio Boards (Firecrawl) ===")
        vc_stats: dict = {}
        try:
            with _stage_timeout("vc_boards", STAGE_BUDGETS["vc_boards"]):
                # Read Firecrawl config directly from env. Avoids pulling
                # full Settings (which requires unrelated secrets) inside
                # the stage block — keeps the stage cheap to dry-run from
                # tests that only fixture the env vars they need.
                fc_base = (os.environ.get("FIRECRAWL_BASE_URL") or "").rstrip("/")
                fc_key = os.environ.get("FIRECRAWL_API_KEY") or ""
                if not fc_base:
                    logger.info(
                        "Stage 1.7 skipped: FIRECRAWL_BASE_URL not set"
                    )
                    stage_outcomes["vc_boards"] = "skipped"
                else:
                    vc_client = VCFirecrawlClient(base_url=fc_base, api_key=fc_key)
                    vc_jobs_by_board = scrape_all_vc_boards(vc_client)
                    total_scraped = sum(len(v) for v in vc_jobs_by_board.values())
                    # loguru uses `{}` placeholders, NOT printf-style. The
                    # earlier `%d %d` form printed literally and made the
                    # stage look like 0 boards / 0 jobs.
                    logger.info(
                        "Stage 1.7 scrape: {} boards, {} total job rows",
                        len(vc_jobs_by_board), total_scraped,
                    )
                    if total_scraped > 0:
                        try:
                            from wekruit_matching.scraper.upsert import (
                                mark_stale_jobs as _mark_stale_vc,
                                upsert_jobs as _upsert_vc,
                            )
                            from wekruit_matching.scraper.upsert import (
                                STALE_GUARD_TRIPPED as _VC_GUARD_TRIPPED,
                            )
                            with get_connection() as conn:
                                for board_slug, jobs in vc_jobs_by_board.items():
                                    if not jobs:
                                        continue
                                    repo_slug = f"vcboard:{board_slug}"
                                    upsert_stats = _upsert_vc(jobs, conn)
                                    seen_ids = {j.job_id for j in jobs}
                                    stale_count = _mark_stale_vc(
                                        seen_ids, repo_slug, conn
                                    )
                                    if stale_count == _VC_GUARD_TRIPPED:
                                        errors.append(
                                            f"Stage 1.7 stale-guard tripped for "
                                            f"{repo_slug}: partial render, skipped "
                                            f"deactivation"
                                        )
                                    vc_stats[repo_slug] = {
                                        **upsert_stats,
                                        "stale": stale_count,
                                    }
                        except Exception as e:
                            logger.error("Stage 1.7 upsert crashed: {}", e)
                            errors.append(f"Stage 1.7 upsert: {e}")
                    stage_outcomes["vc_boards"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "vc_boards", e)
            stage_outcomes["vc_boards"] = "timeout"

        # Merge vc_stats into scrape_stats so the email + webhook see them.
        for k, v in vc_stats.items():
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
                # Degenerate-result guard: attempted rows but resolved none, or a
                # DB-pool/infra route in failed_by_source, means JD fetching is
                # effectively down — not a clean "did-not-raise" night.
                _jd_failed = int(jd_stats.get("failed", 0) or 0)
                _jd_processed = int(jd_stats.get("processed", 0) or 0)
                _jd_by_source = jd_stats.get("failed_by_source", {}) or {}
                _jd_infra = any(
                    k in _jd_by_source for k in ("connection_error", "db_error", "pool_timeout")
                )
                if _jd_infra or (_jd_failed > 0 and _jd_processed == 0):
                    _flag_degraded(
                        "jd_enrich",
                        f"JD fetching degraded: processed={_jd_processed} "
                        f"failed={_jd_failed} by_source={dict(_jd_by_source)}",
                        stage_outcomes=stage_outcomes,
                        errors=errors,
                    )
                else:
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

        # --- Stage 2.5: ATS resolve (jobright -> direct ATS url via Serper) ---
        # Re-introduced 2026-05-30 (fix #2). For active jobs whose primary_url is
        # a jobright redirect and that have no ats_apply_url, resolve the direct
        # ATS apply URL via Serper and write it back with a bumped content_hash.
        # MUST run BEFORE Stage 3 (embed) and Stage 4 (sync): the content_hash
        # bump is what the Firestore receiver keys its re-upsert on, and Stage 4
        # now also re-selects content_hash-only changes (fix #4) so the resolved
        # url reaches the live matcher this same run. Gated on SERPER_API_KEY via
        # env (mirrors Stage 1.7's FIRECRAWL_BASE_URL gate) — when unset the
        # stage is a no-op skip. Best-effort: a failure here never blocks
        # embed/sync/gate (each later stage is its own inline try/except).
        logger.info("=== Stage 2.5: ATS Resolve (Serper) ===")
        try:
            _serper_key = os.environ.get("SERPER_API_KEY") or ""
            if not _serper_key:
                logger.info("Stage 2.5 skipped: SERPER_API_KEY not configured")
                stage_outcomes["ats_resolve"] = "skipped"
            elif resolve_jobright_pending is None:
                logger.info("Stage 2.5 skipped: resolver import unavailable")
                stage_outcomes["ats_resolve"] = "skipped"
            else:
                with _stage_timeout("ats_resolve", STAGE_BUDGETS["ats_resolve"]):
                    ats_stats = resolve_jobright_pending()
                    logger.info("ATS resolve stats: {}", ats_stats)
                    # Do NOT blindly stamp "ok". A dead Serper (out of credits)
                    # resolved 0 jobs for DAYS while this stage reported ok and
                    # fired zero alerts. Two degradation signals flip the run to
                    # degraded + alert a human:
                    #   1. infra_error: the resolver's circuit-breaker tripped on a
                    #      credit/auth/quota failure (dependency DOWN).
                    #   2. resolve-rate collapse: a non-trivial number of rows were
                    #      queried but NONE resolved (e.g. silent API change) — even
                    #      without an explicit infra error, 0% is a red flag.
                    _resolved = int(ats_stats.get("resolved", 0) or 0)
                    _missed = int(ats_stats.get("missed", 0) or 0)
                    _queried = _resolved + _missed
                    if ats_stats.get("infra_error"):
                        _detail = str(ats_stats.get("infra_detail") or "unknown")
                        msg = (
                            "Stage 2.5 ATS resolve: Serper dependency DOWN "
                            f"({_detail}); {ats_stats.get('aborted', 0)} rows left "
                            "unqueried (not poisoned, will retry)."
                        )
                        logger.error(msg)
                        errors.append(msg)
                        stage_outcomes["ats_resolve"] = "degraded"
                        send_dependency_alert(
                            "Serper (ATS resolver)",
                            _detail,
                            impact=(
                                "New jobright jobs are not getting direct ATS "
                                "apply URLs (users see jobright redirect links). "
                                "Matching is unaffected."
                            ),
                            action="Top up Serper credits / verify SERPER_API_KEY.",
                        )
                    elif _queried >= 50 and _resolved == 0:
                        msg = (
                            "Stage 2.5 ATS resolve: resolve-rate collapse — "
                            f"{_queried} rows queried, 0 resolved (0%). Serper may "
                            "be degraded or its response shape changed."
                        )
                        logger.error(msg)
                        errors.append(msg)
                        stage_outcomes["ats_resolve"] = "degraded"
                        send_dependency_alert(
                            "Serper (ATS resolver)",
                            f"0 of {_queried} queries resolved (0% hit rate)",
                            impact="New jobright jobs are not getting direct ATS apply URLs.",
                            action="Check Serper account status + resolver output.",
                        )
                    else:
                        stage_outcomes["ats_resolve"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "ats_resolve", e)
            stage_outcomes["ats_resolve"] = "timeout"
        except Exception as e:
            logger.error("ATS resolve crashed: {}", e)
            errors.append(f"ATS resolve crash: {e}")
            stage_outcomes["ats_resolve"] = "error"

        # --- Stage 3: Embed ---
        logger.info("=== Stage 3: Embedding ===")
        try:
            with _stage_timeout("embed", STAGE_BUDGETS["embed"]):
                embed_stats = embed_all()
                logger.info("Embed stats: {}", embed_stats)
                # Degenerate-result guard: rows failed but NONE embedded means the
                # embedder is effectively down (OpenAI key revoked / 429 storm).
                # embed_all's drain loop breaks on embedded==0 even when failed>0,
                # so it returns {embedded:0, failed:N} and would otherwise stamp
                # 'ok'. The Stage 5 backlog floor only catches this once coverage
                # decays; flag it at the stage so the outage is visible tonight.
                _emb_failed = int(embed_stats.get("failed", 0) or 0)
                _emb_done = int(embed_stats.get("embedded", 0) or 0)
                if _emb_failed > 0 and _emb_done == 0:
                    _flag_degraded(
                        "embed",
                        f"embedding degraded: embedded=0 failed={_emb_failed} "
                        "(embedder may be down — check OpenAI key/quota)",
                        stage_outcomes=stage_outcomes,
                        errors=errors,
                    )
                    send_dependency_alert(
                        "OpenAI (embeddings)",
                        f"0 embedded, {_emb_failed} failed",
                        impact="New/changed jobs are not getting embeddings — they cannot be matched.",
                        action="Check OPENAI_API_KEY + account quota.",
                    )
                else:
                    stage_outcomes["embed"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "embed", e)
            stage_outcomes["embed"] = "timeout"
        except Exception as e:
            logger.error("Embedding crashed: {}", e)
            errors.append(f"Embedding crash: {e}")
            stage_outcomes["embed"] = "error"

        # --- Stage 3.5: Reconcile dead/404 jobs to inactive (pre-sync) ---
        # dead_backfill (Stage 0) + the JD-404 path set dead/permanent_404
        # WITHOUT flipping status, and upsert only SKIPS dead rows from its
        # input. Left alone, confirmed-dead postings stay status='active' and
        # sync to the live matcher as clickable matches that 404. Flip them to
        # inactive HERE (after all dead-marking, before sync) so Stage 4's
        # inactive-sync removes them from Firestore. Status-only; the 90-day
        # dead-retry in upsert still re-activates genuinely re-listed jobs.
        logger.info("=== Stage 3.5: Reconcile dead/404 -> inactive ===")
        try:
            with get_connection() as conn:
                reconciled = reconcile_dead_inactive(conn)
            logger.info("Dead/404 reconciled to inactive: {}", reconciled)
            stage_outcomes["reconcile_dead"] = "ok"
        except Exception as e:
            logger.error("Dead reconcile crashed: {}", e)
            errors.append(f"Dead reconcile crash: {e}")
            stage_outcomes["reconcile_dead"] = "error"

        # --- WS-B Stage 3.6: BLOCKING pre-sync data-quality gate (Gate-4) ---
        # The post-run reliability gate (Stage 5) runs AFTER sync — too late to
        # stop a corrupt batch from reaching the live matcher. This gate runs
        # AFTER embed + dead-reconcile and BEFORE the Firestore sync: if any
        # absolute matching-ready invariant is violated (stamp-without-verify,
        # embedded-without-vector, thin-JD-with-source) or the matchable corpus
        # dropped below the persisted floor, it RAISES PreSyncGateError and we
        # SKIP the sync stage so the bad/regressed data never propagates. The
        # rest of the run still completes (Postgres stays current); the next
        # clean run syncs. Skipped entirely when sync is already being skipped
        # (preflight degrade) — there is nothing to gate.
        if not skip_sync:
            logger.info("=== Stage 3.6: Pre-Sync Data-Quality Gate ===")
            try:
                with get_connection() as conn:
                    assert_pre_sync_ready(conn)
                stage_outcomes["pre_sync_gate"] = "ok"
            except PreSyncGateError as e:
                # Corrupt/regressed data — keep it OUT of Firestore. Skip sync,
                # record as an error so the run is 'partial' (not 'success').
                logger.error("Pre-sync gate BLOCKED sync: {}", e)
                errors.append(f"Pre-sync gate blocked sync: {e}")
                stage_outcomes["pre_sync_gate"] = "blocked"
                skip_sync = True
            except Exception as e:  # noqa: BLE001 - gate compute failure is non-fatal
                # If the gate itself cannot compute (transient DB error, etc.)
                # we do NOT skip sync: the alembic-0010 CHECK constraints already
                # make the worst corruption states unrepresentable at the DB
                # level, and the post-run gate (Stage 5) still runs. Record it
                # in stage_outcomes (NOT in `errors`) so it surfaces without
                # flipping pipeline_status — mirrors Stage 4.5's non-fatal path.
                logger.warning(
                    "Pre-sync gate could not run (non-fatal, proceeding to "
                    "sync): {}", e
                )
                stage_outcomes["pre_sync_gate"] = "error"

        # --- Stage 4: Sync active/inactive jobs to Firebase ---
        # Guarded by skip_sync: a degraded sync credential (Stage 0) OR a
        # tripped pre-sync gate (Stage 3.6) skips this stage so the night still
        # refreshes Postgres without shipping bad/credential-less data.
        if skip_sync:
            logger.warning(
                "=== Stage 4: Firebase Job Sync SKIPPED (skip_sync set) ==="
            )
            stage_outcomes["sync"] = "skipped"
        else:
            logger.info("=== Stage 4: Firebase Job Sync ===")
            try:
                with _stage_timeout("sync", STAGE_BUDGETS["sync"]):
                    sync_stats = sync_jobs_to_firebase(since=run_started_at, full_sync=False)
                    logger.info("Firebase sync stats: {}", sync_stats)
                    # Terminally-rejected docs (400/413/422 after bisection) are
                    # NOT delivered to the matcher this run. Rows stay re-selectable
                    # via the watermark (not data-loss), but a non-zero count is a
                    # partial delivery worth a human's eyes — don't bury it in
                    # job_sync's logger.error.
                    _skipped_docs = int(sync_stats.get("skipped_docs", 0) or 0)
                    if _skipped_docs > 0:
                        _flag_degraded(
                            "sync",
                            f"{_skipped_docs} docs terminally rejected and not "
                            "delivered this run (will retry next run)",
                            stage_outcomes=stage_outcomes,
                            errors=errors,
                        )
                    else:
                        stage_outcomes["sync"] = "ok"
            except StageTimeoutError as e:
                _record_timeout(errors, "sync", e)
                stage_outcomes["sync"] = "timeout"
            except Exception as e:
                logger.error("Job sync crashed: {}", e)
                errors.append(f"Job sync crash: {e}")
                stage_outcomes["sync"] = "error"

        # --- Stage 4.5: Firestore <-> Postgres reconcile (fix #5) ---
        # Distinct from Stage 5's PG-side data-quality FLOORS: this is a
        # cross-store reconcile that detects DIVERGENCE between the PG matchable
        # set and what is ACTUALLY in the live Firestore matching-jobs
        # collection (the true FS-vs-PG active drift — FS active has been
        # observed ~4k higher than PG active because stale-active docs were never
        # flipped). Strongest available signal: if google-cloud-firestore + creds
        # are available, count the live collection (server-side COUNT
        # aggregation) and compare to the PG matchable count; if the Firestore
        # read is unavailable, fall back to comparing the PG matchable count
        # against what Stage 4 sync REPORTED as active_jobs this run. >5%
        # divergence -> LOUD warning + stage_outcomes['firestore_reconcile']=
        # 'degraded'. NEVER fatal (does not append to `errors`, so
        # pipeline_status is unaffected), but always surfaces in stage_outcomes.
        # Never prints service-account contents.
        logger.info("=== Stage 4.5: Firestore <-> Postgres Reconcile ===")
        try:
            with _stage_timeout(
                "firestore_reconcile", STAGE_BUDGETS["firestore_reconcile"]
            ):
                with get_connection() as conn:
                    recon = firestore_reconcile(conn, threshold=0.05)
                if recon.get("skipped"):
                    # Firestore read unavailable — fall back to the count Stage 4
                    # reported as active this run vs the PG matchable set.
                    pg_n = recon.get("pg_matchable", 0)
                    sent_n = sync_stats.get("active_jobs", 0)
                    # Incremental sync sends only a window, so sent < pg is
                    # expected and NOT divergence; only flag if sync claims to
                    # have sent MORE active docs than PG considers matchable (a
                    # real inconsistency), beyond the threshold.
                    denom = max(pg_n, 1)
                    over = max(sent_n - pg_n, 0) / denom
                    if over > 0.05:
                        logger.warning(
                            "FIRESTORE RECONCILE DEGRADED (fallback): sync "
                            "reported active_jobs={} but PG matchable={} "
                            "(over {:.1%} > 5%); reason={}",
                            sent_n, pg_n, over, recon.get("reason"),
                        )
                        stage_outcomes["firestore_reconcile"] = "degraded"
                    else:
                        logger.info(
                            "Firestore reconcile: skipped live read ({}); "
                            "fallback PG matchable={} sync active_jobs={} OK",
                            recon.get("reason"), pg_n, sent_n,
                        )
                        stage_outcomes["firestore_reconcile"] = "skipped"
                elif not recon.get("ok"):
                    logger.warning(
                        "FIRESTORE RECONCILE DEGRADED: {} (PG matchable={}, "
                        "Firestore active={}, total={})",
                        recon.get("reason"),
                        recon.get("pg_matchable"),
                        recon.get("fs_active"),
                        recon.get("fs_total"),
                    )
                    stage_outcomes["firestore_reconcile"] = "degraded"
                else:
                    logger.success(
                        "Firestore reconcile OK: PG matchable={} ~ Firestore "
                        "active={} (divergence {:.1%})",
                        recon.get("pg_matchable"),
                        recon.get("fs_active"),
                        recon.get("divergence", 0.0),
                    )
                    stage_outcomes["firestore_reconcile"] = "ok"
        except StageTimeoutError as e:
            _record_timeout(errors, "firestore_reconcile", e)
            stage_outcomes["firestore_reconcile"] = "timeout"
        except Exception as e:
            # Non-fatal: a reconcile failure must not gate the run. Record it as
            # 'error' in stage_outcomes (NOT in `errors`) so it surfaces without
            # flipping pipeline_status.
            logger.warning("Firestore reconcile crashed (non-fatal): {}", e)
            stage_outcomes["firestore_reconcile"] = "error"

        # --- Stage 5: Post-run reliability gate (P-REL) ---
        # Computes coverage/reconciliation metrics from the live DB and FAILS
        # the run when data quality regressed (embedded-coverage cliff,
        # matchable-corpus drop vs prior, embeddable-but-unembedded backlog,
        # field-coverage regression) -- even when every stage above
        # "succeeded" without raising. This is the failure mode users were
        # discovering by hand ("a new issue every day"). Fail-CLOSED: if the
        # gate itself errors we record it as a failure so it stays visible,
        # never silently green.
        logger.info("=== Stage 5: Reliability Gate (data-quality) ===")
        try:
            with _stage_timeout("health_gate", STAGE_BUDGETS["health_gate"]):
                gate = run_health_gate()
                health_metrics = gate.get("metrics", {}) or {}
                health_failures = gate.get("failures", []) or []
                # Non-blocking warnings (e.g. sponsorship "can't tell" drift) go
                # to dependency_errors so they're surfaced/logged WITHOUT flipping
                # the run to partial — honors "if it can't tell, it's fine".
                for _w in gate.get("warnings", []) or []:
                    dependency_errors.append(
                        f"health-gate warning [{_w.get('metric', '?')}]: "
                        f"{_w.get('message', '')}"
                    )
                if gate.get("ok"):
                    stage_outcomes["health_gate"] = "ok"
                else:
                    stage_outcomes["health_gate"] = "failed"
                    errors.append(
                        "Reliability gate: "
                        + "; ".join(
                            f.get("message", f.get("metric", "?"))
                            for f in health_failures
                        )
                    )
                    logger.error(
                        "Reliability gate FAILED with {} data-quality "
                        "failure(s)",
                        len(health_failures),
                    )
        except StageTimeoutError as e:
            _record_timeout(errors, "health_gate", e)
            stage_outcomes["health_gate"] = "timeout"
        except Exception as e:
            logger.error("Reliability gate crashed (fail-closed): {}", e)
            errors.append(f"Reliability gate crash: {e}")
            stage_outcomes["health_gate"] = "error"
            health_failures = [
                {
                    "metric": "health_gate",
                    "value": "error",
                    "threshold": "n/a",
                    "message": f"reliability gate raised: {e}",
                }
            ]

    except _PreflightAbort:
        # Stage 0 hard fail (missing required core dependency). No stage ran;
        # fall through to the always-fire finalizer so the completion email +
        # the pipelineStatus sentinel still emit. core_ok == 0 -> status
        # 'failed'. The error is already recorded in `errors`.
        logger.error("Aborting run: Stage 0 preflight hard fail")

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
            _ats_resolved = int(ats_stats.get("resolved", 0) or 0)
            _ats_missed = int(ats_stats.get("missed", 0) or 0)
            send_pipeline_complete_email(
                scrape_stats=scrape_stats,
                jd_stats=jd_stats,
                enrich_stats=enrich_stats,
                embed_stats=embed_stats,
                duration_seconds=duration,
                errors=errors,
                stale_jobs=stale_jobs,
                url_resolution_stats={
                    "total_resolved": _ats_resolved,
                    "resolution_rate": _ats_resolved / max(_ats_resolved + _ats_missed, 1),
                    "infra_error": ats_stats.get("infra_error", 0),
                },
                health_failures=health_failures,
                health_metrics=health_metrics,
                stage_outcomes=stage_outcomes,
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
            # Degraded/failed/timeout stage count — daily-update.sh greps this +
            # the stageOutcome lines so the webhook (operator's pager) carries the
            # degradation reason, not just the status. Before this, stage_outcomes
            # were printed but discarded before the webhook left the box.
            _degraded = sorted(
                f"{s}={v}" for s, v in stage_outcomes.items()
                if v in ("degraded", "error", "timeout")
            )
            print(f"degradedStages={len(_degraded)}")
            # Stage outcomes for diagnostic purposes (parsed by daily-update.sh).
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
        # `dependency_errors` are best-effort failures from external services
        # (per-source scrape 5xx, Firecrawl/Serper hiccups, transient Postgres
        # statement timeouts on auxiliary scrapers). They are logged at WARN
        # but do NOT gate pipeline_status — surfaced here purely for
        # diagnostic purposes.
        "dependency_errors": dependency_errors,
        "duration_seconds": duration,
        "pipeline_status": pipeline_status,
        "stage_outcomes": stage_outcomes,
        # P-REL — post-run reliability gate results.
        "health_metrics": health_metrics,
        "health_failures": health_failures,
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
    dep_errs = result.get("dependency_errors") or []
    if dep_errs:
        logger.info(
            "Dependency degradations (non-blocking, {} entries): {}",
            len(dep_errs),
            dep_errs,
        )
    if result["errors"]:
        logger.warning("Errors: {}", result["errors"])
        # Exit non-zero on partial OR failed so launchd surfaces it; the
        # wrapper still grabs the precise status from stdout token.
        sys.exit(1)
