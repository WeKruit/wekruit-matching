---
phase: 07-feedback-loop
plan: 01
subsystem: feedback
tags: [feedback, tdd, record_feedback, affinity_embedding, psycopg3, pgvector]
dependency_graph:
  requires:
    - "src/wekruit_matching/db/connection.py (get_connection)"
    - "src/wekruit_matching/models/feedback.py (ReactionType)"
    - "pgvector.psycopg.register_vector"
  provides:
    - "record_feedback() function callable by any client"
    - "from wekruit_matching import record_feedback (package root export)"
  affects:
    - "src/wekruit_matching/__init__.py (added record_feedback to __all__)"
    - "DB: feedback table (INSERT), user_profiles table (UPDATE liked/disliked/affinity)"
tech_stack:
  added:
    - numpy (already present — used for 70/30 affinity blend math)
    - pgvector.psycopg.register_vector (already present — used for vector codec)
  patterns:
    - "TDD: RED (failing tests) -> GREEN (implementation) -> commit each"
    - "conn injection pattern: if conn is not None use it, else get_connection()"
    - "register_vector(conn) called once per _run() invocation"
    - "ON CONFLICT DO NOTHING for idempotent feedback inserts"
key_files:
  created:
    - src/wekruit_matching/feedback/__init__.py
    - src/wekruit_matching/feedback/handler.py
    - tests/test_feedback_handler.py
  modified:
    - src/wekruit_matching/__init__.py
decisions:
  - "ON CONFLICT DO NOTHING on feedback INSERT — same reaction can be re-recorded safely without raising (ix_feedback_user_job covers user_id + job_id)"
  - "register_vector patched in tests — all 8 tests use patch('wekruit_matching.feedback.handler.register_vector') matching matcher.py test pattern"
  - "No commit inside record_feedback — commit is caller's responsibility, matches enrichment/worker.py and embedding/worker.py patterns"
  - "70/30 blend: 0.7 * existing + 0.3 * new_signal, then normalize with 1e-9 epsilon"
  - "First like sets affinity directly to job embedding (unit-norm from OpenAI, no renormalization needed)"
  - "Applied reaction: only inserts feedback row, no profile side effects"
metrics:
  duration: "~3 minutes 20 seconds"
  completed_date: "2026-03-26T03:17:14Z"
  tasks_completed: 2
  tests_added: 8
  files_created: 3
  files_modified: 1
---

# Phase 07 Plan 01: Feedback Handler Summary

## One-liner

`record_feedback()` implementation with 70/30 affinity blending, idempotent INSERT, and full mocked unit test coverage via TDD.

## What Was Built

### Task 1: `record_feedback()` with full unit tests (TDD)

**RED phase** — wrote 8 failing tests in `tests/test_feedback_handler.py` covering all behavioral branches. Tests were verified to fail with `ModuleNotFoundError` (module didn't exist yet).

**GREEN phase** — implemented `src/wekruit_matching/feedback/handler.py`:

- `record_feedback(user_id, job_id, reaction, conn=None)` — public API
- `_run(...)` — internal implementation on a provided connection
- `_handle_like(...)` — appends to liked_companies, blends affinity_embedding
- `_handle_dislike(...)` — appends to disliked_companies

Key implementation details:
- `INSERT INTO feedback ... ON CONFLICT DO NOTHING` — idempotent re-recording
- `register_vector(conn)` called once at the top of `_run()` — enables pgvector codec for the connection lifetime
- Affinity blending: first like sets affinity directly (OpenAI embeddings are already unit-norm); subsequent likes blend 70% existing + 30% new signal, then re-normalize with epsilon `1e-9`
- `applied` reaction inserts feedback row only — no profile update
- No-op affinity update when `job_row["embedding"] is None` — company still appended to liked_companies
- Conn injection pattern matches `matcher.py`: `if conn is not None use it, else get_connection()`

### Task 2: Package root re-export

Updated `src/wekruit_matching/__init__.py`:
- Added `from wekruit_matching.feedback.handler import record_feedback`
- Added `record_feedback` to `__all__`
- `from wekruit_matching import record_feedback, get_matches` now works

## Test Results

```
8 passed in 0.26s (tests/test_feedback_handler.py)
23 passed — feedback + matcher + models tests combined
```

All 8 tests pass with mocked DB connections — no real Postgres required.

## Commits

| Commit | Type | Description |
|--------|------|-------------|
| 11f52de | test | RED phase — 8 failing tests for record_feedback() |
| 4d0d23c | feat | GREEN phase — feedback package + handler implementation |
| 716fe09 | feat | Re-export record_feedback from package root |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] register_vector() rejects MagicMock — tests needed patch**

- **Found during:** Task 1 GREEN phase (first test run)
- **Issue:** `register_vector(conn)` calls `TypeInfo.fetch()` which checks `isinstance(conn, Connection)` — MagicMock fails this check with `TypeError`
- **Fix:** Updated all 8 tests to use `patch("wekruit_matching.feedback.handler.register_vector")`. This matches the exact pattern used in `test_matching_matcher.py` (`patch("wekruit_matching.matching.matcher.register_vector")`).
- **Files modified:** `tests/test_feedback_handler.py`
- **Impact:** Zero behavior change — register_vector is correctly called in production; tests mock it to avoid needing a real psycopg connection object.

## Known Stubs

None — all DB operations are real SQL using Postgres array_append and vector cast syntax. No placeholder data or hardcoded empty values.

## Pre-existing Failures (Out of Scope)

`tests/test_scraper_parser.py::test_internships_returns_four_jobs` fails with 0/4 jobs parsed. This is a pre-existing Phase 02 scraper regression, unrelated to Phase 07. Logged to `deferred-items.md`.

## Self-Check: PASSED

Files exist:
- src/wekruit_matching/feedback/__init__.py — FOUND
- src/wekruit_matching/feedback/handler.py — FOUND
- tests/test_feedback_handler.py — FOUND

Commits exist:
- 11f52de — FOUND (test RED phase)
- 4d0d23c — FOUND (feat GREEN phase)
- 716fe09 — FOUND (feat re-export)

Import verification: `from wekruit_matching import get_matches, record_feedback` — OK
