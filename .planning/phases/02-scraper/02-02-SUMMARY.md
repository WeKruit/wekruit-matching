---
phase: 02-scraper
plan: "02"
subsystem: scraper
tags: [markdown-parsing, html-stripping, edge-cases, tdd, fixtures, emoji-filtering, continuation-rows]

# Dependency graph
requires:
  - phase: 02-scraper-01
    provides: "generate_job_id(), compute_content_hash(), normalize_company_name() from id_utils"
  - phase: 01-foundation
    provides: "Job pydantic model, JobStatus enum"
provides:
  - "parse_readme(content: bytes, source_repo: str) -> list[Job]: full SimplifyJobs README parser"
  - "tests/fixtures/internships_snapshot.md: parser fixture with all 4 edge case row types"
  - "tests/fixtures/new_grad_snapshot.md: new grad format fixture"
affects: [02-scraper-03-upsert, 03-enrichment]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "html.parser.HTMLParser subclass for inline HTML cell stripping (no external dependency)"
    - "Line-based markdown table extraction — split on | with boundary empty cell removal"
    - "State machine: header_seen + separator_seen flags to enter data rows safely"
    - "Continuation row tracking via last_company variable reset per non-continuation row"
    - "TDD: write failing tests, implement to GREEN"

key-files:
  created:
    - src/wekruit_matching/scraper/parser.py
    - tests/test_scraper_parser.py
    - tests/fixtures/internships_snapshot.md
    - tests/fixtures/new_grad_snapshot.md
  modified: []

key-decisions:
  - "Line-based table extraction over mistune AST — SimplifyJobs format is well-defined; line splitting on | is simpler and more reliable for this specific structure"
  - "stdlib html.parser.HTMLParser for HTML stripping — no additional dependency needed; handles <details>/<summary>/<br>/<strong>/<a> correctly"
  - "Normalize company_name in parser output (lowercase via normalize_company_name) — consistent with id_utils expectation; tests confirm globex/acme corp/initech output"
  - "Regex removes 'N locations' summary artifact from <details><summary> content — summary text is redundant; only location names belong in location_raw"

patterns-established:
  - "Fixture files in tests/fixtures/ loaded via Path(__file__).parent / 'fixtures' — no conftest.py fixtures needed for simple bytes loading"
  - "_HTMLStripper collects text nodes in list then joins with ', ' — handles <br>-separated location lists correctly"
  - "Table state reset on non-| lines — handles multiple tables in one README cleanly"

requirements-completed: [SCRP-03, SCRP-04, SCRP-05]

# Metrics
duration: 5min
completed: 2026-03-26
---

# Phase 2 Plan 02: README Parser Summary

**Line-based SimplifyJobs README parser with stdlib HTML stripping for multi-location cells, lock row exclusion, and continuation row company inheritance**

## Performance

- **Duration:** ~5 min
- **Completed:** 2026-03-26
- **Tasks:** 2
- **Files created:** 4

## Accomplishments

- `parse_readme(content: bytes, source_repo: str) -> list[Job]` handles all three parser-level edge cases from PITFALLS.md
- HTML-embedded multi-location cells (`<details><summary>3 locations</summary>Austin, TX<br>Seattle, WA<br>Remote</details>`) produce clean `location_raw = "Austin, TX, Seattle, WA, Remote"` — no raw HTML tags in output (SCRP-03)
- Lock emoji rows (`🔒 Locked Inc`) are excluded at parse time — never enter the output list (SCRP-04)
- Continuation rows (`↳`) inherit `company_name` from the most recent non-continuation row — no `↳` or empty company in output (SCRP-05)
- 9 new unit tests covering all edge cases plus determinism and new grad format
- Integration smoke test: `parse_readme` on internships fixture returns exactly 4 jobs (Acme, Globex DS, Globex ML, Initech) with all assertions passing

## Task Commits

Each task was committed atomically:

1. **Task 1: Parser fixtures** - `ccde66e` (chore) — `tests/fixtures/internships_snapshot.md`, `tests/fixtures/new_grad_snapshot.md`
2. **RED phase: failing tests** - `a0c477b` (test) — `tests/test_scraper_parser.py` (9 tests, all failing)
3. **GREEN phase: parser implementation** - `e5f47d3` (feat) — `src/wekruit_matching/scraper/parser.py`

## Files Created/Modified

- `src/wekruit_matching/scraper/parser.py` — `parse_readme()` with `_HTMLStripper`, `_strip_html()`, `_extract_url()`, lock/continuation detection
- `tests/test_scraper_parser.py` — 9 tests: job count, HTML stripping, continuation inheritance, lock exclusion, hash format, source_repo, determinism, new grad format
- `tests/fixtures/internships_snapshot.md` — Fixture with standard, HTML multi-location, continuation, lock, and plain rows
- `tests/fixtures/new_grad_snapshot.md` — Fixture with standard and HTML multi-location rows

## Decisions Made

- Line-based table extraction over mistune AST parsing — the plan recommended this approach; SimplifyJobs format is deterministic and well-structured
- `html.parser.HTMLParser` (stdlib) for inline HTML stripping — no additional dependency; correctly handles all HTML patterns in SimplifyJobs README
- `normalize_company_name()` applied in `_parse_company_name()` — lowercase output consistent with ID generation; tests validate `globex`, `acme corp`, `initech`
- Regex `r"\d+\s+locations?,?\s*"` removes "N locations" summary artifact from `<details>` cells

## Deviations from Plan

None — plan executed exactly as written. The implementation code provided in the plan was used without modification.

## Issues Encountered

None.

## Known Stubs

None — all data flows are wired. `parse_readme` produces real `Job` objects with real `job_id` and `content_hash` values from `id_utils`.

## Next Phase Readiness

- `parse_readme()` is ready for Plan 03 (upsert pipeline) — call with raw bytes from `fetch_readme()` and repo slug
- `Job` objects have all fields populated that Plan 03 upsert needs: `job_id`, `content_hash`, `source_repo`, `company_name`, `role_title`, `primary_url`, `location_raw`, `date_posted_raw`
- Full test suite: 36 passed, 5 skipped (DB tests, expected — need live DB)

---
*Phase: 02-scraper*
*Completed: 2026-03-26*

## Self-Check: PASSED

- FOUND: src/wekruit_matching/scraper/parser.py
- FOUND: tests/test_scraper_parser.py
- FOUND: tests/fixtures/internships_snapshot.md
- FOUND: tests/fixtures/new_grad_snapshot.md
- Commits confirmed: ccde66e, a0c477b, e5f47d3
