---
phase: 01-foundation
plan: "02"
subsystem: database
tags: [psycopg3, pgvector, sqlalchemy, alembic, hnsw, postgres, migrations]

# Dependency graph
requires:
  - "01-01 (pydantic config with get_settings, database_url field)"
provides:
  - "psycopg3 ConnectionPool via get_pool() and get_connection() context manager"
  - "SQLAlchemy 2.x table definitions for jobs, user_profiles, feedback (with Vector(1536))"
  - "alembic migration infrastructure with env.py reading DATABASE_URL from pydantic-settings"
  - "Initial migration 0001 creating 3 tables + HNSW index on jobs.embedding with vector_cosine_ops"
  - "Schema smoke tests verifying DB state post-migration"
affects: [phase-2-scraper, phase-3-enrichment, phase-4-embeddings, phase-5-filters, phase-6-scoring, phase-7-feedback]

# Tech tracking
tech-stack:
  added:
    - "psycopg[pool] (psycopg_pool 3.3.0 — ConnectionPool)"
    - "pgvector.sqlalchemy.Vector — SQLAlchemy column type for vector(1536)"
    - "alembic 1.18.x — migration management with autogenerate target_metadata"
    - "pgvector/pgvector:pg16 Docker container — local Postgres with pgvector"
  patterns:
    - "db module pattern: lru_cache get_pool() + @contextmanager get_connection() for pool lifecycle"
    - "URL conversion: _sqlalchemy_url_to_libpq() strips postgresql+psycopg:// prefix for ConnectionPool"
    - "Migration pattern: manual op.execute() for pgvector HNSW index (SQLAlchemy can't express operator classes)"
    - "HNSW index: USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64)"
    - "Test pattern: SET enable_seqscan=OFF to force planner to use HNSW index in small-table tests"

key-files:
  created:
    - "src/wekruit_matching/db/__init__.py"
    - "src/wekruit_matching/db/connection.py"
    - "src/wekruit_matching/db/tables.py"
    - "alembic.ini"
    - "alembic/env.py"
    - "alembic/script.py.mako"
    - "alembic/README"
    - "alembic/versions/0001_initial_schema.py"
    - "tests/test_db_schema.py"
  modified:
    - "pyproject.toml (added psycopg[pool] extra)"
    - "uv.lock"

key-decisions:
  - "psycopg[pool] extra required separately from psycopg[binary] to get psycopg_pool module"
  - "HNSW index defined in migration via op.execute() not in SQLAlchemy table definition — SQLAlchemy cannot express pgvector operator classes declaratively"
  - "Use SET enable_seqscan=OFF in cosine index test — pgvector planner prefers seq scan for small tables even with valid HNSW index; disabling is the standard testing pattern"
  - "alembic.ini sqlalchemy.url left empty — DATABASE_URL injected dynamically in env.py via pydantic-settings"
  - "Started pgvector/pgvector:pg16 Docker container as local Postgres instance (no system postgres installed)"

patterns-established:
  - "DB pool pattern: get_pool() singleton with lru_cache + get_connection() context manager for caller safety"
  - "Migration pattern: manual migration file for schema with pgvector (no autogenerate for initial schema)"
  - "Index testing: SET enable_seqscan=OFF before EXPLAIN ANALYZE to force planner to use HNSW"

requirements-completed: [FOUND-02, FOUND-03, FOUND-04, FOUND-06, FOUND-07]

# Metrics
duration: 12min
completed: 2026-03-26
---

# Phase 01 Plan 02: Database Schema and Alembic Migration Summary

**psycopg3 ConnectionPool + SQLAlchemy table definitions for jobs/user_profiles/feedback + alembic migration with HNSW vector_cosine_ops index on jobs.embedding; all 5 schema smoke tests pass.**

## Performance

- **Duration:** ~12 minutes
- **Completed:** 2026-03-26
- **Tasks:** 2 of 2
- **Files created/modified:** 11

## Accomplishments

- Created `db/` module with `get_pool()` (lru_cache ConnectionPool) and `get_connection()` context manager — the single access point for all DB work in subsequent phases
- Defined SQLAlchemy 2.x table schemas for the three core tables: `jobs` (19 columns including `Vector(1536)` embedding), `user_profiles` (12 columns including `Vector(1536)` affinity embedding), and `feedback` (5 columns with FK constraints)
- Initialized alembic with env.py reading DATABASE_URL from pydantic-settings — no hardcoded URLs in config files
- Created migration `0001` that builds the full schema including the HNSW index (`ix_jobs_embedding_hnsw`) with `vector_cosine_ops` and `m=16, ef_construction=64` — the exact index that Phase 4 cosine similarity queries depend on
- All 15 tests pass (10 from plan 01-01 + 5 new schema smoke tests)

## Task Commits

1. **Task 1: psycopg3 connection pool and SQLAlchemy table definitions** — `1711e8a` (feat)
2. **Task 2: TDD RED — failing schema smoke tests** — `fda4e82` (test)
3. **Task 2: TDD GREEN — alembic init, initial migration, all tests passing** — `4671a4b` (feat)

## Files Created/Modified

- `src/wekruit_matching/db/__init__.py` — Exports get_pool, get_connection, metadata, table objects
- `src/wekruit_matching/db/connection.py` — psycopg3 ConnectionPool with lru_cache singleton and context manager
- `src/wekruit_matching/db/tables.py` — SQLAlchemy 2.x Table definitions: jobs (19 cols), user_profiles (12 cols), feedback (5 cols)
- `alembic.ini` — Alembic config with empty sqlalchemy.url (set dynamically via env.py)
- `alembic/env.py` — Migration environment reading DATABASE_URL from pydantic-settings, target_metadata = db.tables.metadata
- `alembic/versions/0001_initial_schema.py` — Full schema migration: 3 tables, content_hash index, HNSW vector index
- `tests/test_db_schema.py` — 5 integration tests: tables exist, embedding column type, HNSW index, INSERT, cosine query plan
- `pyproject.toml` — Added psycopg[pool] extra

## Decisions Made

- `psycopg[pool]` must be added explicitly as a separate extra from `psycopg[binary]`; `ConnectionPool` lives in the `psycopg_pool` module that only ships with the pool extra
- HNSW index operator class (`vector_cosine_ops`) cannot be expressed in SQLAlchemy `Table()` declarations — it's a pgvector-specific extension to CREATE INDEX syntax, so the index lives in the migration via `op.execute()` raw SQL
- `SET enable_seqscan=OFF` in the cosine query test is the correct testing approach for small tables — the Postgres cost-based planner legitimately prefers seq scan for 15 rows even when an HNSW index exists; disabling seq scan verifies the index is present and usable without needing thousands of rows
- Started a `pgvector/pgvector:pg16` Docker container as the local Postgres instance (no system Postgres found on the machine)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Added psycopg[pool] dependency**
- **Found during:** Task 1 (import verification)
- **Issue:** `psycopg_pool` module not importable — `psycopg[binary]` was in pyproject.toml but pool functionality requires the separate `psycopg[pool]` extra
- **Fix:** `uv add "psycopg[pool]"` — adds psycopg-pool 3.3.0 to lock file
- **Files modified:** `pyproject.toml`, `uv.lock`
- **Commit:** `1711e8a`

**2. [Rule 1 - Bug] Fixed cosine index test for small-table planner behavior**
- **Found during:** Task 2 (GREEN — test_cosine_query_uses_index failed)
- **Issue:** Plan specified "assert Seq Scan not in plan_text" with 15 rows, but pgvector's Postgres planner legitimately chooses seq scan over HNSW for small tables. Test was checking the wrong thing — the goal is to verify the HNSW index is usable, not that the planner always chooses it.
- **Fix:** Changed assertion to `assert "Index Scan" in plan_text` and added `SET enable_seqscan = OFF` before the EXPLAIN ANALYZE to force planner to use HNSW index when it exists
- **Files modified:** `tests/test_db_schema.py`
- **Commit:** `4671a4b`

## Known Stubs

None — no UI rendering, no placeholder data flowing to callers. The connection pool connects to a real DB or raises. The table definitions are complete and match the migration exactly.

## Self-Check: PASSED
