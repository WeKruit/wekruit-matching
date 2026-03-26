---
phase: 05-hard-filters
plan: 01
subsystem: matching
tags: [python, pure-python, filters, location-normalization, job-type, sponsorship, pgvector, hnsw]

# Dependency graph
requires:
  - phase: 01-foundation
    provides: UserProfile and JobType models in wekruit_matching.models.user_profile
  - phase: 02-scraper
    provides: jobs.source_repo, jobs.sponsorship, jobs.location_raw columns (dict shape from DB)
  - phase: 03-llm-enrichment
    provides: sponsorship bool field populated by classifier
  - phase: 04-embeddings
    provides: ANN retrieval candidate set (apply_hard_filters receives ANN output)
provides:
  - "apply_hard_filters(jobs: list[dict], profile: UserProfile) -> list[dict] — post-ANN hard filter chain"
  - "normalize_location(loc: str) -> str — canonical location bucket resolution"
  - "LOCATION_ALIASES dict[str, str] — SF/NYC/LA/Remote/Seattle/Austin/Boston/Chicago coverage"
  - "filter_by_job_type, filter_by_sponsorship, filter_by_location — individual filter functions"
affects: [06-scoring, 07-feedback, 08-api]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "post-ANN filter chain: apply_hard_filters(jobs, profile) -> list[dict] receives raw ANN results"
    - "location canonical buckets: normalize_location() resolves aliases before comparison"
    - "remote-is-universal: remote in job OR remote in preference -> match always"
    - "pure Python filter chain: no DB imports, importable without Postgres"

key-files:
  created:
    - src/wekruit_matching/matching/__init__.py
    - src/wekruit_matching/matching/filters.py
    - tests/test_matching_filters.py
  modified: []

key-decisions:
  - "post-ANN filter chain order: job_type -> sponsorship -> location (job_type narrows most aggressively first)"
  - "LOCATION_ALIASES canonical buckets: SF, NYC, LA, Remote, Seattle, Austin, Boston, Chicago and their common variants"
  - "remote-is-universal: location_raw='Remote' matches any non-empty preferred list; preferred='Remote' matches all jobs"
  - "_job_location_buckets splits on semicolon then comma to handle multi-city location_raw values"
  - "sponsorship filter: only sponsorship=True passes when requires_sponsorship=True; False and None both excluded"

patterns-established:
  - "apply_hard_filters(jobs: list[dict], profile: UserProfile) -> list[dict] — Phase 6 scorer must call this before scoring"
  - "filter functions accept list[dict] and a single parameter, return list[dict] — composable chain pattern"
  - "normalize_location(loc) -> str — Phase 6 may reuse for location_fit score computation"

requirements-completed: [MTCH-01, MTCH-02, MTCH-03]

# Metrics
duration: 3min
completed: 2026-03-26
---

# Phase 05 Plan 01: Hard Filters Summary

**Pure-Python post-ANN filter chain (job_type -> sponsorship -> location) with canonical location alias resolution covering SF/NYC/LA/Remote/Seattle/Austin/Boston/Chicago.**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-26T02:45:50Z
- **Completed:** 2026-03-26T02:48:50Z
- **Tasks:** 2 completed
- **Files modified:** 3 created

## Accomplishments

- Built `src/wekruit_matching/matching/filters.py` with 6 public functions and LOCATION_ALIASES covering all required city aliases
- Implemented `apply_hard_filters()` as the canonical entry point for Phase 6 scoring engine
- Wrote 37 behavioral tests covering all three filter types plus chained apply_hard_filters; zero DB or API key dependency

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: import scaffold** - `2a8220c` (test)
2. **Task 1 GREEN: matching package + filters.py** - `0a99e4b` (feat)
3. **Task 2 GREEN: full behavioral test suite** - `e5cb1db` (test)
4. **Cleanup: remove TDD scaffold** - `d9b9367` (chore)

_TDD: RED commit then GREEN commit per task._

## Files Created/Modified

- `src/wekruit_matching/matching/__init__.py` - Package stub with module docstring
- `src/wekruit_matching/matching/filters.py` - LOCATION_ALIASES, normalize_location, _job_location_buckets, filter_by_job_type, filter_by_sponsorship, filter_by_location, apply_hard_filters; no DB imports
- `tests/test_matching_filters.py` - 37 tests: TestNormalizeLocation, TestFilterByJobType, TestFilterBySponsorship, TestFilterByLocation, TestApplyHardFilters

## Decisions Made

- **Filter chain order:** job_type -> sponsorship -> location. JobType filters most aggressively (binary repo match) so it runs first, shrinking the list before the finer-grained filters run.
- **LOCATION_ALIASES canonical buckets:** SF, NYC, LA, Remote, Seattle, Austin, Boston, Chicago with all common variants (City, ST format; abbreviations; full name).
- **Remote-is-universal:** If a job's `location_raw` resolves to "remote", it matches any non-empty preference list. If a user's preferred_locations includes "Remote", all jobs pass. This ensures remote-friendly users and remote jobs are never incorrectly filtered.
- **`_job_location_buckets` splits on ";" then ",":** SimplifyJobs listings sometimes list multiple cities with semicolons; splitting both separators handles the full range of formats.
- **Sponsorship filter excludes None:** `sponsorship=None` means unknown — treated as "does not offer" when `requires_sponsorship=True`. Only explicit `True` passes.

## Deviations from Plan

None - plan executed exactly as written.

## Known Stubs

None - all filter functions are fully implemented and wired.

## Self-Check: PASSED

Files verified:

```
FOUND: src/wekruit_matching/matching/__init__.py
FOUND: src/wekruit_matching/matching/filters.py
FOUND: tests/test_matching_filters.py
```

Commits verified:

```
FOUND: d9b9367
FOUND: e5cb1db
FOUND: 0a99e4b
FOUND: 2a8220c
```

Tests: 37 passed, 0 failed.
