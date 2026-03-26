---
phase: 03-llm-enrichment
plan: "02"
subsystem: enrichment
tags: [psycopg3, anthropic, loguru, postgres, content-hash-gating, tdd]

# Dependency graph
requires:
  - phase: 03-llm-enrichment-01
    provides: classify_job(job) -> EnrichmentResult, EnrichmentResult model with controlled vocabularies
  - phase: 02-scraper
    provides: upsert_jobs(), get_connection(), Job model, jobs table schema

provides:
  - enrich_pending(conn) -> dict[str, int]: queries unenriched jobs, calls classify_job, writes enrichment results
  - enriched_at IS NULL gating: only processes jobs that have never been enriched (or whose content changed)
  - per-job failure isolation: single classify_job exception increments failed counter, batch continues
  - upsert enriched_at clearing: ON CONFLICT now clears enriched_at when content_hash changes
  - python -m wekruit_matching.enrichment.run: standalone CLI entrypoint for cron scheduling

affects: [04-embeddings, 05-matching, scraper-pipeline]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - enrich_pending(conn) takes a psycopg3 Connection; caller manages get_connection() context
    - per-job commit pattern: conn.commit() after each successful UPDATE for partial-progress preservation
    - enriched_at IS NULL as the sole gate for enrichment eligibility (upsert clears on hash change)
    - CLI entrypoint pattern: enrich_all() + if __name__ == "__main__" block, identical to scraper/run.py

key-files:
  created:
    - src/wekruit_matching/enrichment/worker.py
    - src/wekruit_matching/enrichment/run.py
    - tests/test_enrichment_worker.py
  modified:
    - src/wekruit_matching/scraper/upsert.py

key-decisions:
  - "commit after each successful job write (not batched) — partial batch progress is preserved on failure"
  - "enriched_at IS NULL is the sole worker gate — upsert.py CASE expression clears it on content_hash change"
  - "per-job failure isolation: classify_job exception increments failed counter without aborting the batch"

patterns-established:
  - "Enrichment CLI pattern: enrich_all() wraps enrich_pending(conn) with get_connection(); __main__ block enables cron"
  - "Content-hash re-enrichment: upsert ON CONFLICT sets enriched_at=NULL when content_hash changes"

requirements-completed: [ENRC-05]

# Metrics
duration: 2min
completed: 2026-03-26
---

# Phase 3 Plan 02: Enrichment Worker Summary

**Postgres enrichment worker with content-hash gating — queries `enriched_at IS NULL`, calls `classify_job` per job, commits per-row, isolates failures, and exposes `python -m wekruit_matching.enrichment.run` as a cron entrypoint**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-26T02:17:10Z
- **Completed:** 2026-03-26T02:19:32Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments

- Implemented `enrich_pending(conn)` that queries `WHERE enriched_at IS NULL AND status = 'active'`, classifies each job via `classify_job()`, and writes `industry`, `company_size`, `required_skills`, `sponsorship`, `enriched_at` to the DB with per-row commits
- Added per-job failure isolation: any exception in classify_job or the DB write logs a warning, increments `failed`, and continues to the next job — the batch never aborts on a single failure
- Patched `upsert.py` ON CONFLICT clause: `enriched_at = CASE WHEN content_hash changes THEN NULL ELSE jobs.enriched_at END` — ensures changed jobs automatically re-enter the enrichment queue on the next run
- Created `run.py` CLI entrypoint following the same `enrich_all() + __main__` pattern as `scraper/run.py`
- Added 4 DB integration tests (skip without `DATABASE_URL`): skip-already-enriched, enrich-unenriched, continue-after-failure, and upsert-clears-enriched_at-on-hash-change

## Task Commits

Each task was committed atomically:

1. **Task 1: DB worker with content-hash gating and per-job failure isolation** - `3888f43` (feat)
2. **Task 2: CLI entrypoint run.py for cron scheduling** - `00d1055` (feat)

_Note: Task 1 used TDD (RED: tests written before implementation, GREEN: implementation made tests pass/skip)_

## Files Created/Modified

- `src/wekruit_matching/enrichment/worker.py` - `enrich_pending(conn)`: queries unenriched jobs, calls classify_job, writes results with per-row commits and per-job failure isolation
- `src/wekruit_matching/enrichment/run.py` - `enrich_all()` orchestrator + `__main__` CLI entrypoint for cron scheduling
- `tests/test_enrichment_worker.py` - 4 DB integration tests (skip without DATABASE_URL), all mocking classify_job
- `src/wekruit_matching/scraper/upsert.py` - Added `enriched_at = CASE ... THEN NULL` to ON CONFLICT clause

## Decisions Made

- Per-row `conn.commit()` after each successful UPDATE (not batched): preserves partial progress — if the worker crashes mid-batch, already-enriched jobs are persisted
- `enriched_at IS NULL` is the sole gating condition in the worker query — the re-enrichment mechanism lives entirely in `upsert.py`'s CASE expression, keeping the worker simple
- Per-job failure isolation without rollback: on any exception (DB write or classify_job), the current job is skipped (failed counter incremented) and the batch continues — no transaction to roll back since we commit after each job

## Deviations from Plan

None — plan executed exactly as written. The upsert.py patch was explicitly specified in Task 1 behavior.

## Issues Encountered

- 6 pre-existing failures in `tests/test_scraper_parser.py` confirmed to exist before this plan's changes (verified via `git stash`). These are out of scope — logged here for tracking.

## Known Stubs

None — all code paths are fully wired. `enrich_pending` reads from real DB, calls real `classify_job`, writes real DB rows. No hardcoded empty values flowing to any output.

## User Setup Required

None — no external service configuration required beyond `DATABASE_URL` and `ANTHROPIC_API_KEY` (both established in prior phases).

## Next Phase Readiness

- Full enrichment cycle operational: scrape → upsert → enrich (via `python -m wekruit_matching.enrichment.run`)
- Phase 04 (embeddings) can now read `industry`, `company_size`, `required_skills`, `sponsorship` from enriched job rows
- Content-hash gating ensures Phase 04 embedding generation can use `embedded_at IS NULL` as its analogous gate

---
*Phase: 03-llm-enrichment*
*Completed: 2026-03-26*
