---
phase: 02-scraper
plan: "01"
subsystem: scraper
tags: [httpx, github-api, sha256, emoji-normalization, tdd, rate-limiting]

# Dependency graph
requires:
  - phase: 01-foundation
    provides: "Settings with github_token field, Job pydantic model with content_hash validator, test isolation pattern (_env_file=None)"
provides:
  - "fetch_readme(repo_slug) -> bytes: authenticated GitHub raw content fetcher with 429 retry"
  - "generate_job_id(company, title, url) -> str: stable 64-char SHA-256 job ID, emoji-safe"
  - "compute_content_hash(company, title) -> str: 64-char SHA-256 for enrichment gating"
  - "normalize_company_name(raw) -> str: unicodedata-based emoji/punctuation normalization"
  - "REPO_INTERNSHIPS and REPO_NEW_GRAD constants for SimplifyJobs repos"
affects: [02-scraper-02-parser, 02-scraper-03-upsert, 03-enrichment, 04-embeddings]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "httpx.get() with Authorization: Bearer header for GitHub PAT auth"
    - "Exponential backoff retry loop (range(3), sleep 2^attempt) inline — no tenacity for simple HTTP retry"
    - "unicodedata.category() for emoji stripping — no hardcoded emoji list"
    - "TDD: write failing tests, implement to GREEN, no REFACTOR needed"
    - "Settings(_env_file=None, ...) fixture pattern for test isolation (established in Phase 01)"
    - "patch('module.get_settings', return_value=settings) to inject test config into fetcher"

key-files:
  created:
    - src/wekruit_matching/scraper/__init__.py
    - src/wekruit_matching/scraper/fetcher.py
    - src/wekruit_matching/scraper/id_utils.py
    - tests/test_scraper_fetcher.py
    - tests/test_scraper_id_utils.py
  modified: []

key-decisions:
  - "No tenacity for fetcher retry — inline loop is cleaner for 3-attempt backoff and gives cleaner error output in tests"
  - "unicodedata categories (So, Sm, Sk, Cs, Cn) for emoji stripping — future-proof vs hardcoded emoji set"
  - "compute_content_hash does NOT normalize company_name — content change detection should be sensitive to actual text changes; normalization is only for ID stability"

patterns-established:
  - "Scraper module isolation: fetcher.py patches get_settings directly via monkeypatch at module level, not via Settings() fixture reloads"
  - "Test 429 retry: mock httpx.get to return 429 N times, patch time.sleep to prevent actual delays, assert call_count == N"

requirements-completed: [SCRP-01, SCRP-02, SCRP-06, SCRP-07, SCRP-09]

# Metrics
duration: 2min
completed: 2026-03-26
---

# Phase 2 Plan 01: Scraper Foundation Summary

**Authenticated GitHub fetcher with PAT + 429 retry, and SHA-256 stable ID utilities with emoji normalization via unicodedata categories**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-26T01:15:43Z
- **Completed:** 2026-03-26T01:18:07Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- fetch_readme() fetches raw README bytes from SimplifyJobs repos with Authorization: Bearer PAT header, retries on 429 (1s/2s/4s backoff), raises HTTPStatusError on 401/403
- generate_job_id() produces identical 64-char SHA-256 hex for "🔥 Google" and "Google" — emoji normalization working
- compute_content_hash() provides stable content fingerprint for Phase 3 enrichment gating (only re-enrich when company+title changes)
- 17 new unit tests across 2 test files — all mocked, no real network or DB calls

## Task Commits

Each task was committed atomically:

1. **Task 1: GitHub authenticated fetcher** - `91b2169` (feat)
2. **Task 2: Stable ID and content hash utilities** - `17982b3` (feat)

## Files Created/Modified
- `src/wekruit_matching/scraper/__init__.py` - Package init for scraper module
- `src/wekruit_matching/scraper/fetcher.py` - fetch_readme() with PAT auth and 429 retry
- `src/wekruit_matching/scraper/id_utils.py` - normalize_company_name(), generate_job_id(), compute_content_hash()
- `tests/test_scraper_fetcher.py` - 6 tests: 2 success paths, 2 error paths (401/403), 1 retry path, 1 constants check
- `tests/test_scraper_id_utils.py` - 11 tests: 5 parametrized normalize cases + 6 ID/hash behavior tests

## Decisions Made
- No tenacity for fetcher retry — inline `for attempt in range(3)` loop is sufficient and gives cleaner test output (no tenacity wrapper obscuring retry count assertions)
- Use `unicodedata.category()` for emoji detection rather than a hardcoded emoji set — handles new emoji added to Unicode without code changes
- `compute_content_hash` intentionally does NOT normalize company_name — normalization is for ID stability; content hash should detect actual text mutations

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered
None.

## User Setup Required

None — no external service configuration required. GITHUB_TOKEN must be set in .env (already in Settings from Phase 01).

## Next Phase Readiness
- fetch_readme() ready for Plan 02 (markdown parser) — call with REPO_INTERNSHIPS or REPO_NEW_GRAD
- generate_job_id() and compute_content_hash() ready for Plan 03 (upsert pipeline)
- All 27 tests pass (5 DB tests skipped — need live DB, expected)

---
*Phase: 02-scraper*
*Completed: 2026-03-26*

## Self-Check: PASSED
