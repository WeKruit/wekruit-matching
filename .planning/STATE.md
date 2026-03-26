---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: Ready to execute
stopped_at: Completed 03-llm-enrichment-01-PLAN.md
last_updated: "2026-03-26T02:16:21.425Z"
progress:
  total_phases: 8
  completed_phases: 2
  total_plans: 7
  completed_plans: 6
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-25)

**Core value:** Given a user profile, return the most relevant job listings ranked by fit
**Current focus:** Phase 03 — LLM Enrichment

## Current Position

Phase: 03 (LLM Enrichment) — EXECUTING
Plan: 2 of 2

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
| Phase 01-foundation P01 | 4 | 2 tasks | 14 files |
| Phase 01-foundation P02 | 12 | 2 tasks | 11 files |
| Phase 02-scraper P01 | 2 | 2 tasks | 5 files |
| Phase 02-scraper P02 | 5 | 2 tasks | 4 files |
| Phase 02-scraper P03 | 3 | 2 tasks | 3 files |
| Phase 03-llm-enrichment P01 | 2 | 2 tasks | 3 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Init: Use psycopg3 (not psycopg2) — psycopg3 is the correct new-project choice per official docs; maintenance-only for psycopg2
- Init: HNSW index with vector_cosine_ops — IVFFlat has worse recall/latency tradeoff for incremental inserts
- Init: Content-hash gate on enrichment is non-negotiable from Phase 3 — missing it costs ~$600/month at 2,000 jobs
- Init: Hard filters applied post-ANN retrieval (not as SQL pre-filters) — pre-filters shrink candidate set and trigger sequential scan
- [Phase 01-foundation]: Use _env_file=None in Settings tests for isolation from project .env file
- [Phase 01-foundation]: Use datetime.now(UTC) via _utcnow() helper; utcnow() is deprecated in Python 3.12+
- [Phase 01-foundation]: Negate .env.example in local .gitignore to override global ~/.gitignore_global .env.* pattern
- [Phase 01-foundation]: psycopg[pool] extra required separately for ConnectionPool (psycopg_pool module)
- [Phase 01-foundation]: HNSW index with vector_cosine_ops defined in migration via op.execute() — SQLAlchemy cannot express pgvector operator classes in Table() declarations
- [Phase 01-foundation]: Use SET enable_seqscan=OFF in HNSW index tests — planner legitimately prefers seq scan for small tables; disable to verify index exists and is usable
- [Phase 02-scraper]: No tenacity for fetcher retry — inline loop is cleaner for 3-attempt backoff and avoids tenacity wrapping errors in test output
- [Phase 02-scraper]: unicodedata.category() for emoji stripping — future-proof vs hardcoded emoji set, handles new Unicode emoji without code changes
- [Phase 02-scraper]: compute_content_hash does NOT normalize company_name — normalization is for ID stability only; content hash should detect actual text mutations
- [Phase 02-scraper]: Line-based markdown table extraction over mistune AST — SimplifyJobs format is well-defined; split on | is simpler and more reliable for this specific structure
- [Phase 02-scraper]: stdlib html.parser.HTMLParser for HTML stripping — no additional dependency; handles all HTML patterns in SimplifyJobs README cells correctly
- [Phase 02-scraper]: CTE approach for old_hash capture in ON CONFLICT upsert — RETURNING sees post-update values so pre-upsert snapshot via CTE is required for accurate insert/update/unchanged classification
- [Phase 02-scraper]: mark_stale_jobs uses separate query path for empty seen_ids — NOT IN () is SQL syntax error; empty set triggers WHERE source_repo = X AND status = 'active' without NOT IN
- [Phase 03-llm-enrichment]: _get_client() returns cached Anthropic client — test-injectable via patch without lru_cache invalidation issues
- [Phase 03-llm-enrichment]: skills_in_vocab validator lowercases before KNOWN_SKILLS intersection — LLM may return mixed case; normalizing at validation time ensures DB consistency
- [Phase 03-llm-enrichment]: classify_job never raises — catches all exceptions and returns safe default (all unknown/null); enrichment worker in Plan 02 handles safe defaults

### Pending Todos

None yet.

### Blockers/Concerns

- Phase 2: Reconcile whether to parse `listings.json` (structured JSON) vs raw README markdown — if listings.json is available it eliminates the HTML parsing complexity entirely. Inspect the SimplifyJobs repo before building the parser.
- Phase 3: Enrichment prompt structure for Claude Haiku on terse listings (company name + role title only) needs empirical tuning before bulk run. Test against 50-100 real listings first.

## Session Continuity

Last session: 2026-03-26T02:16:21.423Z
Stopped at: Completed 03-llm-enrichment-01-PLAN.md
Resume file: None
