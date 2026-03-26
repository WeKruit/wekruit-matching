# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-25)

**Core value:** Given a user profile, return the most relevant job listings ranked by fit
**Current focus:** Phase 1 — Foundation

## Current Position

Phase: 1 of 8 (Foundation)
Plan: 0 of ? in current phase
Status: Ready to plan
Last activity: 2026-03-25 — Roadmap created; 46 v1 requirements mapped across 8 phases

Progress: [░░░░░░░░░░] 0%

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: -
- Total execution time: 0 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| - | - | - | - |

**Recent Trend:**
- Last 5 plans: -
- Trend: -

*Updated after each plan completion*

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Init: Use psycopg3 (not psycopg2) — psycopg3 is the correct new-project choice per official docs; maintenance-only for psycopg2
- Init: HNSW index with vector_cosine_ops — IVFFlat has worse recall/latency tradeoff for incremental inserts
- Init: Content-hash gate on enrichment is non-negotiable from Phase 3 — missing it costs ~$600/month at 2,000 jobs
- Init: Hard filters applied post-ANN retrieval (not as SQL pre-filters) — pre-filters shrink candidate set and trigger sequential scan

### Pending Todos

None yet.

### Blockers/Concerns

- Phase 2: Reconcile whether to parse `listings.json` (structured JSON) vs raw README markdown — if listings.json is available it eliminates the HTML parsing complexity entirely. Inspect the SimplifyJobs repo before building the parser.
- Phase 3: Enrichment prompt structure for Claude Haiku on terse listings (company name + role title only) needs empirical tuning before bulk run. Test against 50-100 real listings first.

## Session Continuity

Last session: 2026-03-25
Stopped at: Roadmap and STATE initialized; no plans created yet
Resume file: None
