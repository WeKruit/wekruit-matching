---
phase: 06-scoring-engine
plan: 01
subsystem: matching
tags: [numpy, cosine-similarity, scoring, weighted-sum, tdd, pytest]

# Dependency graph
requires:
  - phase: 05-hard-filters
    provides: normalize_location, _job_location_buckets, _preferred_buckets from filters.py
  - phase: 01-foundation
    provides: UserProfile, CompanySizePreference models
provides:
  - WEIGHTS constant (7 signals, sums to 1.0)
  - score_title_similarity (cosine similarity via numpy)
  - score_skills_overlap (set intersection, case-insensitive)
  - score_industry_match (exact match or 0.3 fallback)
  - score_company_size_match (exact or 'any' -> 1.0, 0.4 otherwise)
  - score_location_fit (uses filters._job_location_buckets and _preferred_buckets)
  - score_recency (linear decay, 1.0 today, 0.0 at 30 days)
  - score_feedback_boost (1.0 liked / 0.0 disliked / 0.5 cold-start)
  - score_job (combinator: 7 signals -> weighted sum -> score + signals dict)
affects: [07-feedback-loop, matcher-plan-02]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Pure-function signal design: each scorer function takes only primitive inputs, no DB or API calls"
    - "Reuse internal helpers (_job_location_buckets, _preferred_buckets) from filters.py for location scoring"
    - "TDD RED/GREEN: write full test suite first, then implement to make them pass"

key-files:
  created:
    - src/wekruit_matching/matching/scorer.py
    - tests/test_matching_scorer.py
  modified: []

key-decisions:
  - "score_location_fit reuses _job_location_buckets and _preferred_buckets from filters.py — no duplication of 10-line split logic"
  - "WEIGHTS values are plain Python floats that sum to exactly 1.0 — verified by test_weights_sum_to_one"
  - "score_job uses profile.preferred_company_size.value (string) to compare against job dict's company_size string"
  - "feedback_boost cold-start default is 0.5 per MTCH-13 — neutral signal for unknown companies"

patterns-established:
  - "Signal functions are pure: inputs are primitive types only, no ORM models or DB handles passed inside"
  - "score_job is the sole combinator — callers always go through score_job, never call signal functions directly in production"
  - "Test isolation: all 37 tests run without DB, env vars, or API keys"

requirements-completed: [MTCH-04, MTCH-05, MTCH-06, MTCH-07, MTCH-08, MTCH-09, MTCH-10, MTCH-11, MTCH-13]

# Metrics
duration: 2min
completed: 2026-03-26
---

# Phase 6 Plan 01: 7-Signal Weighted Job Scorer Summary

**numpy cosine-similarity scorer with 7 independent signals (title 0.30, skills 0.25, industry 0.15, size 0.10, location 0.10, recency 0.05, feedback 0.05) — pure Python, no DB/API, 37 tests pass**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-26T02:59:06Z
- **Completed:** 2026-03-26T03:01:03Z
- **Tasks:** 2 (RED + GREEN; no REFACTOR needed)
- **Files modified:** 2

## Accomplishments

- Implemented `scorer.py` with WEIGHTS dict (7 keys, sum = 1.0), 7 signal functions, and `score_job()` combinator
- 37 unit tests covering all signals, edge cases (None inputs, empty lists, orthogonal vectors, very old dates), and integration tests for `score_job()` shape and weighted sum correctness
- `score_location_fit` reuses `_job_location_buckets` and `_preferred_buckets` from `filters.py` — no location normalization logic duplicated
- Cold-start feedback boost of 0.5 correctly implemented per MTCH-13

## Task Commits

Each task was committed atomically:

1. **Task 1: RED — failing tests** - `5fb236e` (test)
2. **Task 2: GREEN — implement scorer.py** - `d90fe7b` (feat)

_Note: No REFACTOR commit needed — implementation was clean on first pass._

## Files Created/Modified

- `src/wekruit_matching/matching/scorer.py` — WEIGHTS constant, 7 signal functions, score_job() combinator; no DB/API imports
- `tests/test_matching_scorer.py` — 37 unit tests; all pass without DB connection or API keys; 340 lines

## Decisions Made

- `score_location_fit` reuses `_job_location_buckets` and `_preferred_buckets` from `filters.py` — avoids duplicating the 10-line split + normalize logic
- `score_job` accesses `profile.preferred_company_size.value` (string) for comparison against the job dict's `company_size` string (which is a plain string, not an enum)
- `feedback_boost` cold-start default is 0.5 per MTCH-13 — neutral signal for companies the user has no history with

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None. Pre-existing `test_scraper_parser.py` failures (6 tests) are unrelated to this plan and were present before any changes in this execution.

## Known Stubs

None — all signal values are computed from real inputs; no hardcoded placeholder data flows to any output.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `score_job()` is ready to be called by `matcher.py` (Plan 02) — takes `job dict`, `UserProfile`, and `query_embedding`, returns `{"score": float, "signals": dict}`
- Plan 02 (matcher) can import directly: `from wekruit_matching.matching.scorer import score_job, WEIGHTS`
- No blockers. All requirements MTCH-04 through MTCH-11 and MTCH-13 are satisfied.

---
*Phase: 06-scoring-engine*
*Completed: 2026-03-26*

## Self-Check: PASSED

- FOUND: src/wekruit_matching/matching/scorer.py
- FOUND: tests/test_matching_scorer.py
- FOUND: .planning/phases/06-scoring-engine/06-01-SUMMARY.md
- FOUND: RED commit 5fb236e
- FOUND: GREEN commit d90fe7b
