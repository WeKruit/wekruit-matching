# Phase 07 Deferred Items

## Pre-existing Test Failure (Out of Scope)

**File:** tests/test_scraper_parser.py::test_internships_returns_four_jobs
**Status:** FAIL — pre-existing, not caused by Phase 07 changes
**Symptom:** `parse_readme()` returns 0 jobs from fixture markdown; expected 4
**Root cause:** Scraper parser change (likely Phase 02 regression) — parsing logic
  returns 0 active jobs from the test fixture markdown.
**Impact:** Phase 07 feedback loop is unaffected. This failure predates Phase 07.
**Action needed:** Investigate parser logic for markdown table extraction in a
  future Phase 02 debug session.
