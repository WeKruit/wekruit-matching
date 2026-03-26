---
phase: 04-embeddings
plan: 01
subsystem: api
tags: [openai, embeddings, tenacity, lru_cache, retry, text-embedding-3-small]

# Dependency graph
requires:
  - phase: 01-foundation
    provides: config.py with get_settings() and openai_api_key field
  - phase: 03-llm-enrichment
    provides: classifier.py pattern for _get_client, _should_retry, _call_*, public wrapper function

provides:
  - embed_text(text, client) -> list[float] with tenacity retry (1536-dim OpenAI embeddings)
  - compose_embedding_text(job) -> str canonical "{title} at {company}. Skills: {skills_csv}"
  - EMBEDDING_MODEL = "text-embedding-3-small" constant
  - embedding package at src/wekruit_matching/embedding/

affects:
  - 04-02 (embedding worker that calls embed_text per job)
  - Any future plan that needs to generate or compare job embeddings

# Tech tracking
tech-stack:
  added: []  # openai and tenacity already in pyproject.toml from Phase 3
  patterns:
    - "retry_if_exception(predicate) instead of retry_if_exception_type — allows 4xx pass-through"
    - "lru_cache(maxsize=1) on _get_client() — cached singleton, injectable in tests via patch"
    - "_should_retry_openai(exc) predicate — checks isinstance(exc, RateLimitError) first, then APIStatusError + status_code >= 500"
    - "embed_text(text, client=None) — optional client param defaults to _get_client() so caller passes mock in tests"

key-files:
  created:
    - src/wekruit_matching/embedding/__init__.py
    - src/wekruit_matching/embedding/embedder.py
    - tests/test_embedding_embedder.py
  modified: []

key-decisions:
  - "Use retry_if_exception(_should_retry_openai) not retry_if_exception_type — retry_if_exception_type catches ALL APIStatusError including 4xx; predicate restricts to 429 and 5xx only"
  - "openai.RateLimitError checked before APIStatusError in predicate — RateLimitError is a subclass of APIStatusError in the openai SDK; explicit check ensures correct behavior regardless of SDK internals"
  - "embed_text accepts optional client parameter — avoids needing to patch _get_client() in tests; caller passes mock directly"
  - "Mirror classifier.py structure exactly — _get_client, _should_retry, _call_openai, embed_text — keeps codebase consistent"

patterns-established:
  - "TDD: RED commit (failing tests) then GREEN commit (implementation) — both committed separately"
  - "retry_if_exception(predicate) pattern for fine-grained retry control on OpenAI calls"

requirements-completed: [ENRC-06, ENRC-08]

# Metrics
duration: 2min
completed: 2026-03-26
---

# Phase 4 Plan 01: Embedding Module Summary

**OpenAI text-embedding-3-small wrapper with tenacity retry — retry_if_exception predicate ensures 4xx errors pass through immediately while 429/5xx retry up to 5 times with exponential backoff**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-26T02:29:32Z
- **Completed:** 2026-03-26T02:31:40Z
- **Tasks:** 1 (TDD: 2 commits — test RED + feat GREEN)
- **Files modified:** 3 created

## Accomplishments
- Embedding package at `src/wekruit_matching/embedding/` with `__init__.py` and `embedder.py`
- `compose_embedding_text(job)` returns canonical `"{title} at {company}. Skills: {skills_csv}"` string
- `embed_text(text, client)` wraps OpenAI embeddings API with tenacity retry, returns `list[float]` (1536 dims)
- Retry predicate distinguishes 429/5xx (retry) from 4xx (pass-through immediately) — tested and verified
- 10 unit tests, all passing, no real OpenAI API calls required (fully mocked)

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: Failing tests** - `d18ed9b` (test)
2. **Task 1 GREEN: Embedding module implementation** - `051e61e` (feat)

## Files Created/Modified
- `src/wekruit_matching/embedding/__init__.py` - Package marker (empty)
- `src/wekruit_matching/embedding/embedder.py` - Core module: EMBEDDING_MODEL, compose_embedding_text, _get_client, _should_retry_openai, _call_openai, embed_text
- `tests/test_embedding_embedder.py` - 10 unit tests covering all behaviors

## Decisions Made
- Used `retry_if_exception(_should_retry_openai)` instead of `retry_if_exception_type` — the plan explicitly specified this to ensure 4xx errors are not retried; `retry_if_exception_type((RateLimitError, APIStatusError))` would retry all `APIStatusError` including 400s
- Checked `isinstance(exc, openai.RateLimitError)` before `isinstance(exc, openai.APIStatusError)` in predicate — `RateLimitError` is a subclass of `APIStatusError` in the openai SDK; explicit check ensures intent is clear
- `embed_text` accepts optional `client` parameter defaulting to `_get_client()` — test-injectable without needing `_get_client.cache_clear()` on every test

## Deviations from Plan

None — plan executed exactly as written, including the explicit note about `retry_if_exception` vs `retry_if_exception_type`.

## Issues Encountered
None.

## User Setup Required
None — no external service configuration required. Tests are fully mocked; real embedding calls require `OPENAI_API_KEY` in `.env` which is already documented in Phase 1.

## Next Phase Readiness
- `embed_text()` and `compose_embedding_text()` are ready for the Phase 4 Plan 02 embedding worker
- Worker will: query jobs where `embedded_at IS NULL`, call `compose_embedding_text` + `embed_text`, write vector + `embedding_model` + `embedded_at` back to DB

---
*Phase: 04-embeddings*
*Completed: 2026-03-26*

## Self-Check: PASSED

- FOUND: src/wekruit_matching/embedding/__init__.py
- FOUND: src/wekruit_matching/embedding/embedder.py
- FOUND: tests/test_embedding_embedder.py
- FOUND: .planning/phases/04-embeddings/04-01-SUMMARY.md
- FOUND commit: d18ed9b (test RED)
- FOUND commit: 051e61e (feat GREEN)
