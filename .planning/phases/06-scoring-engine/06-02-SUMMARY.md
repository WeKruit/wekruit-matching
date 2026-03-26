---
phase: 06-scoring-engine
plan: 02
subsystem: matching
tags: [pgvector, psycopg3, openai, numpy, pytest, tdd, ann, embeddings]

# Dependency graph
requires:
  - phase: 06-01
    provides: score_job() and WEIGHTS — 7-signal weighted scoring combinator
  - phase: 05-hard-filters
    provides: apply_hard_filters() — job_type/sponsorship/location filter chain
  - phase: 04-embeddings
    provides: embed_text() — 1536-dim OpenAI embedding with retry
  - phase: 01-foundation
    provides: get_connection() context manager, ConnectionPool with dict_row factory
provides:
  - get_matches() in matcher.py — full ANN retrieval + hard filters + scoring pipeline
  - from wekruit_matching import get_matches — public package-level API (MTCH-12)
affects:
  - Phase 07 (feedback loop — calls get_matches and updates affinity_embedding)
  - Discord bot / web app / any downstream consumer of the matching API

# Tech tracking
tech-stack:
  added: []
  patterns:
    - ANN retrieval with top_n*4 candidate pool before Python-side filtering
    - register_vector(conn) per connection before pgvector ANN query
    - Affinity embedding bypass: if profile.affinity_embedding is not None, skip embed_text
    - result_row = {**job, **score_job(job, profile, query_embedding)} merge pattern
    - Optional conn injection: if conn is not None use it, else get_connection()

key-files:
  created:
    - src/wekruit_matching/matching/matcher.py
    - tests/test_matching_matcher.py
  modified:
    - src/wekruit_matching/__init__.py

key-decisions:
  - "ANN candidate limit is top_n * 4 — balances recall vs. Python-side filter cost"
  - "affinity_embedding bypass skips embed_text entirely — no OpenAI call when Phase 7 affinity vector is present"
  - "Cold-start (no affinity, no skills) falls back to embed_text('software engineer') — feedback_boost returns 0.5 neutral for all companies"
  - "conn injection pattern mirrors embed_text client injection — test-injectable without mocking pool"

patterns-established:
  - "Pattern: Optional conn injection — if conn is not None: use it; else get_connection()"
  - "Pattern: ANN candidate pool = top_n * 4 before Python-side hard filters + scoring"
  - "Pattern: register_vector(conn) called per connection before pgvector vector column reads"
  - "Pattern: result dict merge = {**job_row, **score_result} preserves all DB fields"

requirements-completed: [MTCH-12]

# Metrics
duration: 3min
completed: 2026-03-26
---

# Phase 06 Plan 02: Matching Engine Public API Summary

**pgvector ANN retrieval wired to hard filters and 7-signal scorer, exposed as `get_matches` on the wekruit_matching package root**

## Performance

- **Duration:** ~3 min
- **Started:** 2026-03-26T03:03:06Z
- **Completed:** 2026-03-26T03:05:25Z
- **Tasks:** 2 (1 TDD: RED+GREEN commits)
- **Files modified:** 3

## Accomplishments
- matcher.py: `get_matches()` that fetches top_n*4 ANN candidates via pgvector `<=>`, applies hard filters, scores each with `score_job()`, returns top-N sorted list
- Affinity embedding Phase 7 hook: when `profile.affinity_embedding` is not None, `embed_text` is bypassed entirely
- Cold-start (no skills, no liked/disliked companies, no affinity embedding) works without error
- `from wekruit_matching import get_matches` works at the package root (MTCH-12)
- 8 unit tests with mocked DB and embedder — 0 real DB/API calls in test suite

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: Failing tests** - `ea595c7` (test)
2. **Task 1 GREEN: matcher.py implementation** - `1f724ed` (feat)
3. **Task 2: __init__.py re-export** - `7ee97d6` (feat)

_Note: TDD task has RED and GREEN commits separately._

## Files Created/Modified
- `src/wekruit_matching/matching/matcher.py` - get_matches() with ANN retrieval, hard filters, scoring pipeline
- `tests/test_matching_matcher.py` - 8 unit tests: returns_list, sorted_desc, cold_start, top_n cap, score/signals shape, affinity bypass, field preservation, ANN limit
- `src/wekruit_matching/__init__.py` - Added get_matches re-export + __all__

## Decisions Made
- ANN candidate limit is top_n * 4 — standard over-fetch to leave room for hard filter attrition without starving the top-N result set
- Affinity bypass skips embed_text entirely when Phase 7 affinity vector is present (one OpenAI call per cold-start get_matches invocation, zero when affinity exists)
- conn injection pattern mirrors embed_text client injection — passes conn directly to inner `_run()` function, falls back to pool context manager when None
- register_vector(conn) called at the top of _fetch_ann_candidates — per-connection registration required by pgvector.psycopg before executing vector queries

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None. Pre-existing test_scraper_parser.py failures (6 tests) confirmed as pre-existing via `git stash` check — unrelated to this plan.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- Phase 06 Scoring Engine is now complete (both plans done)
- `from wekruit_matching import get_matches` is the single entry point for all consumers
- Phase 07 (feedback loop) can call `get_matches()` and update `profile.affinity_embedding` — the bypass hook is already in place
- Phase 08 (end-to-end) can drive the full pipeline from scraper through to ranked match results

## Self-Check: PASSED

- FOUND: src/wekruit_matching/matching/matcher.py
- FOUND: tests/test_matching_matcher.py
- FOUND: src/wekruit_matching/__init__.py
- FOUND commit: ea595c7 (test: RED)
- FOUND commit: 1f724ed (feat: GREEN)
- FOUND commit: 7ee97d6 (feat: __init__.py re-export)

---
*Phase: 06-scoring-engine*
*Completed: 2026-03-26*
