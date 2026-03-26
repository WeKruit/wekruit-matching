---
phase: 02-scraper
plan: "03"
subsystem: scraper
tags: [upsert, idempotency, stale-marking, on-conflict, tdd, orchestrator, cli]

# Dependency graph
requires:
  - phase: 02-scraper-01
    provides: "fetch_readme(), REPO_INTERNSHIPS, REPO_NEW_GRAD from fetcher; compute_content_hash() from id_utils"
  - phase: 02-scraper-02
    provides: "parse_readme(content, source_repo) -> list[Job]"
  - phase: 01-foundation
    provides: "get_connection() context manager, jobs table with ON CONFLICT (job_id) primary key"
provides:
  - "upsert_jobs(jobs: list[Job], conn: psycopg.Connection) -> dict[str, int]: idempotent upsert with insert/update/unchanged stats"
  - "mark_stale_jobs(seen_ids, source_repo, conn) -> int: per-repo stale marking, never deletes rows"
  - "scrape_all() -> dict[str, dict]: full orchestrator for both repos"
  - "python -m wekruit_matching.scraper.run: standalone CLI entrypoint"
affects: [03-enrichment, 08-integration]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "ON CONFLICT (job_id) DO UPDATE — idempotent upsert with minimal write amplification"
    - "CTE pre-read for old_hash to detect insert vs hash-changed vs unchanged in a single round-trip"
    - "xmax = 0 in RETURNING to detect insert vs update without a prior SELECT"
    - "Per-repo stale marking (WHERE source_repo = X) — prevents cross-repo contamination"
    - "TDD: RED (failing tests committed) → GREEN (implementation) → no refactor needed"

key-files:
  created:
    - src/wekruit_matching/scraper/upsert.py
    - src/wekruit_matching/scraper/run.py
    - tests/test_scraper_upsert.py
  modified: []

key-decisions:
  - "CTE approach for old_hash detection — RETURNING clause in ON CONFLICT sees post-update values for the updated row; using a WITH clause to snapshot the pre-upsert content_hash allows correct insert/update/unchanged classification in one query"
  - "Content-hash-based counter semantics — unchanged means same hash (no meaningful content change), even though last_seen_at is always updated as bookkeeping"
  - "mark_stale_jobs uses tuple(seen_ids) for NOT IN clause — psycopg3 parameterizes this correctly; empty seen_ids uses a separate query path to avoid NOT IN () syntax error"

patterns-established:
  - "Upsert stats return dict {inserted, updated, unchanged} — consistent counter semantics for monitoring and idempotency verification"
  - "scrape_all() returns per-repo stats dict — clean contract for Phase 8 cron wrapper (INTG-02)"
  - "DB integration tests skip via _connect() helper when DATABASE_URL not set — pattern matches test_db_schema.py"

requirements-completed: [SCRP-08]

# Metrics
duration: 3min
completed: 2026-03-26
---

# Phase 2 Plan 03: Upsert Pipeline and Scraper Orchestrator Summary

**Idempotent ON CONFLICT upsert with CTE-based change detection, per-repo stale marking, and scrape_all() orchestrator wiring fetch → parse → upsert → mark_stale end-to-end**

## Performance

- **Duration:** ~3 min
- **Completed:** 2026-03-26
- **Tasks:** 2 (Task 1 with TDD, Task 2 standard)
- **Files created:** 3

## Accomplishments

- `upsert_jobs(jobs, conn)` inserts new jobs, updates changed jobs (content_hash differs), and counts unchanged jobs — all in one SQL round-trip per job using ON CONFLICT + CTE
- `mark_stale_jobs(seen_ids, source_repo, conn)` marks all active jobs from a given repo that did NOT appear in the latest scrape as `status='inactive'` — scoped per `source_repo` to prevent cross-repo contamination (SCRP-08)
- `scrape_all()` orchestrates both repos in sequence: `fetch_readme → parse_readme → upsert_jobs → mark_stale_jobs`, returning per-repo stats
- `python -m wekruit_matching.scraper.run` standalone CLI entrypoint for cron scheduling
- 6 DB integration tests covering all upsert behaviors — skip gracefully when DATABASE_URL is not set
- Full test suite: 36 passed, 11 skipped (all DB tests skip without live DB — expected)

## Task Commits

Each task was committed atomically:

1. **RED phase: failing upsert tests** - `acc63e6` (test) — `tests/test_scraper_upsert.py` (6 tests, all failing on ModuleNotFoundError)
2. **GREEN phase: upsert implementation** - `53cf370` (feat) — `src/wekruit_matching/scraper/upsert.py`
3. **Task 2: orchestrator** - `51c8c03` (feat) — `src/wekruit_matching/scraper/run.py`

## Files Created/Modified

- `src/wekruit_matching/scraper/upsert.py` — `upsert_jobs()` with CTE insert/update/unchanged detection; `mark_stale_jobs()` with per-repo scope
- `src/wekruit_matching/scraper/run.py` — `scrape_all()` orchestrator; `__main__` CLI block
- `tests/test_scraper_upsert.py` — 6 integration tests: insert, no-duplicate, hash-update, noop, stale marking, cross-repo isolation

## Decisions Made

- **CTE for old_hash capture** — In PostgreSQL's ON CONFLICT DO UPDATE, the RETURNING clause exposes post-update column values (not pre-update). To distinguish "hash changed" from "hash same", a WITH clause reads the current `content_hash` before the upsert executes. This avoids a separate SELECT round-trip while giving accurate per-row change classification.

- **Content-hash semantics for unchanged** — The `unchanged` counter tracks cases where `content_hash` is identical (no enrichment-relevant change), even though `last_seen_at` is always refreshed in the UPDATE SET clause. This aligns with the Phase 3 enrichment gate: only jobs with a changed `content_hash` need re-enrichment.

- **Empty seen_ids edge case** — `NOT IN ()` with an empty tuple is a SQL syntax error. `mark_stale_jobs` uses a separate query path when `seen_ids` is empty: `WHERE source_repo = X AND status = 'active'` without a NOT IN clause, marking all active rows from that repo as stale.

## Deviations from Plan

### Auto-improved Implementation

**1. [Rule 1 - Bug] CTE approach instead of xmax-only RETURNING**
- **Found during:** Task 1 implementation
- **Issue:** The plan's proposed `(jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash) AS hash_changed` in RETURNING is evaluated post-update — `jobs.content_hash` refers to the updated (new) value, making it always equal to `EXCLUDED.content_hash`. This would cause all updates to be counted as `unchanged`.
- **Fix:** Added a `WITH old AS (SELECT content_hash FROM jobs WHERE job_id = ...)` CTE that snapshots the pre-upsert hash, then compared `old_hash != new_hash` in application code after the query.
- **Files modified:** `src/wekruit_matching/scraper/upsert.py`
- **Commit:** `53cf370`

## Issues Encountered

None blocking. One plan logic error auto-fixed (see Deviations).

## Known Stubs

None — all data flows are wired. `upsert_jobs` and `mark_stale_jobs` make real SQL calls. `scrape_all` integrates all prior scraper components end-to-end.

## Phase 2 Completion

With this plan, all 3 Phase 2 plans are complete:
- 02-01: fetcher + id_utils (SCRP-01, SCRP-02, SCRP-06, SCRP-07, SCRP-09)
- 02-02: parser (SCRP-03, SCRP-04, SCRP-05)
- 02-03: upsert + orchestrator (SCRP-08)

All 9 Phase 2 requirements (SCRP-01 through SCRP-09) are addressed across the three plans.

## Next Phase Readiness

- Phase 3 (LLM Enrichment) can query `SELECT * FROM jobs WHERE content_hash IS NOT NULL AND enriched_at IS NULL` to find unenriched jobs
- `content_hash` is correctly populated on all inserted rows — Phase 3 enrichment gate is ready to use
- `scrape_all()` is the cron-ready entrypoint for Phase 8 integration wiring

---
*Phase: 02-scraper*
*Completed: 2026-03-26*
