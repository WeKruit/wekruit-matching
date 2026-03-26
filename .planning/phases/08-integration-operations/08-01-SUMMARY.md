---
phase: 08-integration-operations
plan: "01"
subsystem: integration
tags: [e2e, testing, pipeline, smoke-test, public-api]
dependency_graph:
  requires:
    - 07-feedback-loop-01 (record_feedback, feedback handler)
    - 06-scoring-engine-01 (get_matches, matcher)
    - 05-hard-filters-01 (filter chain)
    - 04-embeddings-01 (embed_all, embedding worker)
    - 03-llm-enrichment-01 (enrich_all, enrichment worker)
    - 02-scraper-01 (scrape_all, scraper orchestrator)
  provides:
    - end-to-end pipeline exerciser (scripts/e2e_test.py)
    - public API import smoke tests (tests/test_integration_imports.py)
  affects: []
tech_stack:
  added: []
  patterns:
    - "Full pipeline exerciser script: scrape->enrich->embed->match->feedback in sequence"
    - "Pure introspection tests: no DB calls, verifies importability and function signatures"
key_files:
  created:
    - scripts/__init__.py
    - scripts/e2e_test.py
    - tests/test_integration_imports.py
  modified: []
decisions:
  - "e2e_test.py wraps entire pipeline in try/except with sys.exit(1) on failure — caller gets clear success/failure signal"
  - "TDD smoke tests are purely introspective — no DB required, runs in CI without infrastructure"
  - "scripts/__init__.py added so scripts/ is importable (required for test verify step)"
metrics:
  duration: "~2 minutes"
  completed: "2026-03-26"
  tasks_completed: 2
  tasks_total: 2
  files_created: 3
  files_modified: 0
---

# Phase 08 Plan 01: Integration & Operations — E2E Pipeline and Import Smoke Tests Summary

**One-liner:** End-to-end pipeline exerciser running scrape->enrich->embed->match->feedback in sequence, plus 7 pytest import smoke tests verifying the public API surface (`get_matches`, `record_feedback`, `__version__`).

## What Was Built

### Task 1: End-to-end pipeline script (scripts/e2e_test.py)

A runnable pipeline exerciser (`uv run python scripts/e2e_test.py`) that:

1. **SCRAPE** — calls `scrape_all()`, logs per-repo inserted/updated/unchanged/stale counts; warns if no new/updated jobs
2. **ENRICH** — calls `enrich_all()`, logs enriched/failed counts
3. **EMBED** — calls `embed_all()`, logs embedded/failed counts
4. **MATCH** — builds a test `UserProfile` (user_id=`e2e-test-user`, Python/ML/SQL skills, intern, Remote/SF/NYC) and calls `get_matches(profile, top_n=10)`; prints ranked results with score and location
5. **FEEDBACK** — calls `record_feedback(user_id, job_id, reaction="like")` on first match
6. **SUMMARY** — prints all stage stats and final success line

Wraps all steps in try/except; exits with 1 on any exception, 0 on success. Uses loguru with a clean single handler.

136 lines (min_lines requirement: 80).

### Task 2: Library import smoke tests (tests/test_integration_imports.py)

7 pytest tests verifying the public API surface (all pass, no DB required):

| Test | What it checks |
|------|---------------|
| `test_public_imports` | `get_matches`, `record_feedback`, `__version__` importable |
| `test_get_matches_is_callable` | `get_matches` is callable |
| `test_record_feedback_is_callable` | `record_feedback` is callable |
| `test_version_is_string` | `__version__` is a non-empty string |
| `test_all_exports` | `__all__` == `{"get_matches", "record_feedback", "__version__"}` |
| `test_get_matches_signature` | signature has `profile`, `conn`, `top_n`, `openai_client` |
| `test_record_feedback_signature` | signature has `user_id`, `job_id`, `reaction`, `conn` |

55 lines (under 60-line target).

## Verification Results

- `uv run pytest tests/test_integration_imports.py -v` → 7 passed in 0.36s
- `uv run python -c "from wekruit_matching import get_matches, record_feedback; print('OK')"` → OK
- `uv run python -c "import ast; ast.parse(open('scripts/e2e_test.py').read()); print('syntax OK')"` → syntax OK
- All 5 pipeline functions (`scrape_all`, `enrich_all`, `embed_all`, `get_matches`, `record_feedback`) referenced in e2e_test.py
- `sys.exit(0)` and `sys.exit(1)` both present
- `e2e-test-user` appears 2 times (match call + feedback call)

## Commits

| Task | Commit | Description |
|------|--------|-------------|
| Task 1 | 4a6f1ca | feat(08-01): add end-to-end pipeline exerciser script |
| Task 2 | 5f547b6 | feat(08-01): add library import smoke tests (7 passing) |

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — scripts/e2e_test.py wires real pipeline functions. No placeholder data or hardcoded empty values in rendered output.
