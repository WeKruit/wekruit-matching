---
phase: 16-url-resolution
plan: "03"
subsystem: pipeline
tags: [python, httpx, serper, url-resolution, integration-test, pytest]

# Dependency graph
requires:
  - phase: 16-02
    provides: run_url_resolution.py orchestrator with simplify + slug_registry passes
  - phase: 16-01
    provides: url_resolver.py core module, resolve_simplify_jobs, resolve_via_slug_registry
provides:
  - resolve_via_serper() in url_resolver.py — Serper.dev fallback for unmatched JobRight jobs
  - _extract_serper_url() helper — extracts first ATS link from Serper organic results
  - serper_api_key field in config.py Settings — optional, empty = skip gracefully
  - run_url_resolution updated — calls all 3 passes, returns resolution_rate
  - test_url_resolver_integration.py — 4 integration smoke tests measuring resolution rate
affects:
  - daily.py (calls run_url_resolution — now returns serper stats + resolution_rate)
  - pipeline reporting (resolution_rate added to return dict)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - Graceful-skip pattern for optional API keys (same as mailgun/firecrawl)
    - Resolution rate measurement via DB subquery on latest 1K jobs
    - TDD RED-GREEN for new resolver functions

key-files:
  created:
    - wekruit-matching/tests/test_url_resolver_integration.py
  modified:
    - wekruit-matching/src/wekruit_matching/config.py
    - wekruit-matching/src/wekruit_matching/pipeline/url_resolver.py
    - wekruit-matching/src/wekruit_matching/pipeline/run_url_resolution.py
    - wekruit-matching/tests/test_url_resolver.py
    - wekruit-matching/tests/test_run_url_resolution.py
    - wekruit-matching/pyproject.toml

key-decisions:
  - "resolve_via_serper skips immediately when serper_api_key is empty — same Mailgun/Firecrawl pattern"
  - "0.5s sleep between Serper queries for free-tier rate limit safety (2500/month)"
  - "_ATS_HOSTNAMES_FOR_SERPER tuple covers greenhouse.io, lever.co, ashbyhq.com — Workday excluded (paid)"
  - "Integration test uses rollback (not commit) so test runs never mutate production DB"
  - "pytest.mark.integration registered in pyproject.toml markers to avoid PytestUnknownMarkWarning"
  - "test_run_url_resolution updated to mock get_settings and conn.execute().fetchone() for resolution_rate query"

patterns-established:
  - "Resolution rate measurement: COUNT FILTER WHERE ats_apply_url IS NOT NULL over latest 1K active jobs"
  - "Mock conn for run_url_resolution: use _mock_conn_with_rate_row() helper to satisfy fetchone() for resolution_rate"

requirements-completed:
  - RESOLVE-04

# Metrics
duration: 15min
completed: "2026-04-01"
---

# Phase 16 Plan 03: Serper.dev Fallback + Integration Test Summary

**Serper.dev search fallback (RESOLVE-04) wired as third URL resolution pass, with resolution_rate measurement on 1K jobs and integration smoke test suite**

## Performance

- **Duration:** 15 min
- **Started:** 2026-04-01T04:07:20Z
- **Completed:** 2026-04-01T04:22:00Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments

- `resolve_via_serper()` implemented — for unmatched JobRight jobs, POSTs targeted Serper.dev search query, extracts first greenhouse.io/lever.co/ashbyhq.com result, writes ats_apply_url + jd_fetch_source='serper'
- `serper_api_key` added to Settings as optional field (empty string = skip gracefully, same pattern as mailgun/firecrawl)
- `run_url_resolution.py` updated to call all three passes (simplify → slug_registry → serper when key set) and return `resolution_rate` measuring fraction of latest 1K active jobs with ats_apply_url
- 4 integration smoke tests created in `test_url_resolver_integration.py` — skip gracefully without DATABASE_URL, use rollback to avoid mutating DB during test runs
- Full unit test suite updated and expanded: 22 unit tests pass (added 3 new tests for serper in run_url_resolution, 4 new serper/extract tests in url_resolver)

## Task Commits

1. **TDD RED: failing serper tests** - `c25a200` (test)
2. **GREEN: resolve_via_serper + config + orchestrator** - `a118a11` (feat)
3. **Integration smoke test file** - `5cde40a` (feat)

## Files Created/Modified

- `wekruit-matching/src/wekruit_matching/config.py` — Added `serper_api_key: str = Field("", repr=False)` with comment
- `wekruit-matching/src/wekruit_matching/pipeline/url_resolver.py` — Added `_ATS_HOSTNAMES_FOR_SERPER`, `_extract_serper_url()`, `resolve_via_serper()`
- `wekruit-matching/src/wekruit_matching/pipeline/run_url_resolution.py` — Wired serper pass, added `get_settings()` call, added resolution_rate DB query, updated return dict
- `wekruit-matching/tests/test_url_resolver.py` — Added 4 new tests for serper functions
- `wekruit-matching/tests/test_run_url_resolution.py` — Rewrote to mock get_settings + rate row; added 3 new tests (serper_called, resolution_rate, zero_total)
- `wekruit-matching/tests/test_url_resolver_integration.py` — Created with 4 integration tests
- `wekruit-matching/pyproject.toml` — Registered `integration` pytest marker

## Decisions Made

- `resolve_via_serper` skips immediately when `serper_api_key` is empty — same graceful-skip pattern as mailgun and firecrawl. No crash, no DB access.
- Integration tests use `conn.rollback()` rather than `conn.commit()` so running the test suite never writes to the production DB.
- `pytest.mark.integration` registered in pyproject.toml markers — eliminates PytestUnknownMarkWarning that would have appeared in CI.
- Existing `test_run_url_resolution.py` tests were updated (Rule 1 auto-fix) to mock `get_settings` and provide a proper `fetchone()` return for the resolution_rate query — the new DB query caused loguru to fail formatting MagicMock values.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Existing run_url_resolution tests broke due to new get_settings + resolution_rate DB query**
- **Found during:** Task 1 GREEN verification
- **Issue:** The new `get_settings()` call and the final `conn.execute().fetchone()` for resolution_rate caused 3 existing tests to fail: loguru attempted to format MagicMock dict values (`row["resolved"]`) producing `TypeError: unsupported format string passed to MagicMock.__format__`
- **Fix:** Updated `test_run_url_resolution.py` with `_mock_conn_with_rate_row()` helper that returns proper `{"resolved": N, "total": N}` from `fetchone()`, and added `get_settings` monkeypatching to all tests. Also added 3 new tests covering serper, resolution_rate, and zero-total edge cases.
- **Files modified:** `wekruit-matching/tests/test_run_url_resolution.py`
- **Verification:** All 22 unit tests pass
- **Committed in:** `a118a11` (Task 1 GREEN commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - bug)
**Impact on plan:** Required update was necessary for correctness — new code broke existing test mocks. No scope creep.

## Issues Encountered

None beyond the test mock update documented above.

## User Setup Required

To enable the Serper.dev fallback, add to your `.env`:
```
SERPER_API_KEY=your_key_here
```
Get a free API key at https://serper.dev (2,500 free queries/month). The pipeline runs normally without it — the Serper pass is simply skipped.

## Next Phase Readiness

- RESOLVE-04 complete — all three URL resolution passes implemented and tested
- Phase 16 (url-resolution) is complete: RESOLVE-02 (simplify copy), RESOLVE-03 (slug registry), RESOLVE-04 (Serper fallback)
- Integration test ready to run against live DB: `DATABASE_URL=... uv run pytest tests/test_url_resolver_integration.py -v -m integration`
- Resolution rate baseline measurement ready once integration test is run against production DB

## Self-Check

- [x] `resolve_via_serper` exists at url_resolver.py:388
- [x] `_extract_serper_url` exists at url_resolver.py:372
- [x] `serper_api_key` exists at config.py:40
- [x] `resolution_rate` in run_url_resolution.py:97,110
- [x] test_url_resolver_integration.py created with 4 tests collected
- [x] All 22 unit tests pass
- [x] Commits: c25a200, a118a11, 5cde40a

---
*Phase: 16-url-resolution*
*Completed: 2026-04-01*
