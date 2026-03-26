# Deferred Items — Phase 05 Hard Filters

## Pre-Existing Failures (Out of Scope)

### test_scraper_parser.py::test_internships_returns_four_jobs

- **Discovered during:** Task 2 (full suite regression check)
- **Status:** Pre-existing failure — confirmed failing on commit prior to Phase 5 changes
- **Root cause:** `parse_readme()` returns 0 jobs for Summer2026-Internships fixture; likely the fixture markdown format changed since the test was written
- **Impact:** Unrelated to Phase 5 (matching/filters.py has no dependency on scraper parser)
- **Action needed:** Fix scraper parser test fixture in a future maintenance pass
