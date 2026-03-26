---
phase: quick
plan: 260325-vkr
subsystem: api
tags: [fastapi, uvicorn, rest-api, valet-integration]
dependency_graph:
  requires: [wekruit_matching.matching.matcher, wekruit_matching.feedback.handler, wekruit_matching.db.connection, wekruit_matching.models.user_profile, wekruit_matching.models.feedback]
  provides: [wekruit_matching.api.server.app]
  affects: [valet-integration]
tech_stack:
  added: [fastapi>=0.115, uvicorn>=0.29]
  patterns: [sync FastAPI endpoints for psycopg3 pool compatibility, FeedbackRequest inline model, lazy pool initialization via lru_cache]
key_files:
  created:
    - src/wekruit_matching/api/__init__.py
    - src/wekruit_matching/api/server.py
  modified:
    - pyproject.toml
    - uv.lock
decisions:
  - "Sync def endpoints (not async def): psycopg3 ConnectionPool is synchronous; FastAPI runs sync endpoints in a threadpool automatically, avoiding event-loop blocking without needing asyncio.to_thread"
  - "No lifespan startup/shutdown hooks: get_pool() is lru_cached and initializes lazily on first request — correct for internal VALET integration server"
  - "No auth middleware: internal service for VALET only"
  - "FeedbackRequest defined inline in server.py: keeps the HTTP contract co-located with the endpoint that uses it"
metrics:
  duration: "~1 minute"
  completed: "2026-03-26T03:47:00Z"
  tasks: 2
  files: 4
---

# Quick Task 260325-vkr: Build FastAPI REST API for VALET Integration — Summary

**One-liner:** Thin FastAPI HTTP layer wrapping the existing matching engine as three REST endpoints (POST /match, POST /feedback, GET /jobs/stats) with fastapi + uvicorn added to the project via uv.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Add fastapi and uvicorn to dependencies | 77eb4b6 | pyproject.toml, uv.lock |
| 2 | Create FastAPI server with three endpoints | 32e3048 | src/wekruit_matching/api/__init__.py, src/wekruit_matching/api/server.py |

## What Was Built

### API Server (`src/wekruit_matching/api/server.py`)

FastAPI app (`app`) with three endpoints:

**POST /match**
- Accepts `UserProfile` as JSON body (FastAPI + Pydantic v2 native)
- Calls `get_matches(profile)` — uses default `top_n=30`, pool-managed connection
- Returns `{"matches": [...]}` wrapping the ranked job dict list
- Catches all exceptions and raises `HTTPException(500, detail=str(e))`

**POST /feedback**
- Accepts `FeedbackRequest` body: `{user_id: str, job_id: str, reaction: ReactionType}`
- Calls `record_feedback(body.user_id, body.job_id, body.reaction)` — connection/commit handled internally
- Returns `{"status": "ok"}`
- Catches all exceptions and raises `HTTPException(500, detail=str(e))`

**GET /jobs/stats**
- Opens connection via `with get_connection() as conn:`
- Executes `SELECT source_repo, status, COUNT(*) AS count FROM jobs GROUP BY source_repo, status ORDER BY source_repo, status`
- Returns `{"stats": [{"source_repo": ..., "status": ..., "count": ...}, ...]}`
- Catches all exceptions and raises `HTTPException(500, detail=str(e))`

### Start Command

```bash
uv run uvicorn wekruit_matching.api.server:app
```

OpenAPI docs at http://127.0.0.1:8000/docs

## Deviations from Plan

None — plan executed exactly as written.

## Known Stubs

None — all three endpoints are fully wired to their respective backend functions. The GET /jobs/stats endpoint will return an empty list `{"stats": []}` if the jobs table is empty, which is correct behavior (not a stub).

## Self-Check: PASSED

- [x] `src/wekruit_matching/api/__init__.py` exists
- [x] `src/wekruit_matching/api/server.py` exists and exports `app`
- [x] `from wekruit_matching.api.server import app; print(app.title)` prints "WeKruit Matching API"
- [x] POST /match, POST /feedback, GET /jobs/stats all registered in app.routes
- [x] fastapi and uvicorn importable via `uv run python -c "import fastapi; import uvicorn; print('ok')"`
- [x] Commit 77eb4b6 (Task 1) confirmed in git log
- [x] Commit 32e3048 (Task 2) confirmed in git log
