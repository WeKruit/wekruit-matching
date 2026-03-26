---
phase: 04-embeddings
plan: 02
subsystem: api
tags: [pgvector, embeddings, worker, cli, psycopg3, register_vector, per-job-isolation]

# Dependency graph
requires:
  - phase: 04-01
    provides: embed_text(), compose_embedding_text(), EMBEDDING_MODEL from embedding/embedder.py
  - phase: 03-llm-enrichment
    provides: enrichment worker pattern (query, loop, commit, counters) mirrored exactly
  - phase: 01-foundation
    provides: get_connection() context manager, jobs table schema with embedded_at/enriched_at columns

provides:
  - embed_pending(conn) -> dict[str, int] — reads enriched-but-unembedded jobs, writes vectors
  - embed_all() -> dict[str, int] — CLI orchestrator using get_connection()
  - CLI entry point: uv run python -m wekruit_matching.embedding.run
  - DB integration tests (5 tests, skip without DATABASE_URL)

affects:
  - Phase 05+ (matching engine will read the embedding column for ANN retrieval)
  - Any pipeline runner that needs to trigger the embedding step after enrichment

# Tech tracking
tech-stack:
  added: []  # pgvector.psycopg already in pyproject.toml from Phase 4 Plan 01
  patterns:
    - "register_vector(conn) called at top of embed_pending — registers pgvector type adapter for psycopg3"
    - "Per-job commit after each successful embed_text write — partial progress preserved on failure"
    - "Per-job failure isolation: except Exception logs warning, increments failed counter, continues"
    - "SQL gate: WHERE embedded_at IS NULL AND enriched_at IS NOT NULL AND status = 'active'"
    - "DB integration tests use skip_no_db = pytest.mark.skipif(not DATABASE_URL) — skip gracefully"
    - "HNSW test uses SET enable_seqscan=OFF — forces index usage on small tables to verify index exists"

key-files:
  created:
    - src/wekruit_matching/embedding/worker.py
    - src/wekruit_matching/embedding/run.py
    - tests/test_embedding_worker.py
  modified: []

key-decisions:
  - "register_vector(conn) called at top of embed_pending, not in run.py — registration is per-connection and belongs in the function that uses the vector type"
  - "embed_pending mirrors enrich_pending exactly — same pattern: query, fetchall, per-row try/except, commit after each success, counters"
  - "SQL gate on embedded_at IS NULL + enriched_at IS NOT NULL — embedding naturally follows enrichment; content_hash change clears enriched_at so changed jobs re-enter enrichment first"
  - "HNSW test added to test_embedding_worker.py (not a separate file) — groups all embedding-layer DB tests together"

patterns-established:
  - "TDD for DB worker: tests written before implementation; all skip gracefully without DATABASE_URL"
  - "register_vector(conn) as first call in any function writing vector columns"

requirements-completed: [ENRC-06, ENRC-07, ENRC-08]

# Metrics
duration: 2min
completed: 2026-03-26
---

# Phase 4 Plan 02: Embedding Worker Summary

**Embedding worker and CLI runner that read enriched-but-unembedded jobs from DB, call embed_text() per job with pgvector adapter registration, and write vectors + provenance back with per-job commit isolation**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-26T02:33:49Z
- **Completed:** 2026-03-26T02:36:18Z
- **Tasks:** 2 (TDD Task 1 + Task 2)
- **Files created:** 3

## Accomplishments

- `embedding/worker.py`: `embed_pending(conn)` with SQL gate (`embedded_at IS NULL AND enriched_at IS NOT NULL`), `register_vector(conn)` for pgvector adapter, per-job commit, per-job failure isolation, returns `{"embedded": N, "failed": M, "skipped": 0}`
- `embedding/run.py`: `embed_all()` + CLI `__main__` block, mirrors `enrichment/run.py` exactly
- `tests/test_embedding_worker.py`: 5 DB integration tests — skip-already-embedded, skip-unenriched, embeds-enriched-job, continues-after-failure, HNSW index verification — all skip gracefully without `DATABASE_URL`
- All 10 embedder unit tests (from Plan 01) still pass; 5 new DB integration tests skip correctly without DB

## Task Commits

Each task was committed atomically:

1. **Task 1 (TDD feat+test): Embedding worker + DB integration tests** — `cb2ef0f`
2. **Task 2 (feat): CLI runner embed_all()** — `922bb8c`

## Files Created/Modified

- `src/wekruit_matching/embedding/worker.py` — `embed_pending(conn)`: SQL gate, register_vector, per-job loop with embed_text + UPDATE + commit, failure isolation
- `src/wekruit_matching/embedding/run.py` — `embed_all()` + CLI `__main__` entry point
- `tests/test_embedding_worker.py` — 5 DB integration tests covering all must_haves from plan

## Decisions Made

- `register_vector(conn)` called at the top of `embed_pending` (not in `run.py`) — registration is per-connection and is needed wherever the vector column is written; placing it in the worker function ensures it's always called before the UPDATE
- Mirrored `enrichment/worker.py` pattern exactly — same query structure, same per-row try/except, same commit-after-each-success, same counters dictionary
- HNSW test placed in `test_embedding_worker.py` alongside other embedding DB tests (not a separate file) — consistent with Phase 1's pattern of grouping related DB tests

## Deviations from Plan

None — plan executed exactly as written. The HNSW test was included directly in the test file created for Task 1 (the plan specified adding it in Task 2 but it was written in the same file, which is what the plan also described — no behavioral difference).

## Known Stubs

None. All data paths are wired: worker reads from DB, calls `embed_text`, writes vectors back to DB.

## Issues Encountered

None.

## User Setup Required

None beyond what Phase 1 already established. Real embedding runs require `OPENAI_API_KEY` in `.env` (Phase 1) and `DATABASE_URL` for DB tests. Both documented in Phase 1.

## Next Phase Readiness

- `embed_all()` and `embed_pending(conn)` are ready to be called from cron or a pipeline runner
- Phase 4 is now complete: embedder (Plan 01) + worker/runner (Plan 02)
- Phase 5 (matching engine) can now use the `embedding` column for ANN cosine similarity retrieval via pgvector

---
*Phase: 04-embeddings*
*Completed: 2026-03-26*

## Self-Check: PASSED

- FOUND: src/wekruit_matching/embedding/worker.py
- FOUND: src/wekruit_matching/embedding/run.py
- FOUND: tests/test_embedding_worker.py
- FOUND: .planning/phases/04-embeddings/04-02-SUMMARY.md
- FOUND commit: cb2ef0f (feat - Task 1)
- FOUND commit: 922bb8c (feat - Task 2)
