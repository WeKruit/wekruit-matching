---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: Ready to plan
stopped_at: Completed 07-feedback-loop-01-PLAN.md
last_updated: "2026-03-26T03:21:30.720Z"
progress:
  total_phases: 8
  completed_phases: 7
  total_plans: 13
  completed_plans: 13
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-25)

**Core value:** Given a user profile, return the most relevant job listings ranked by fit
**Current focus:** Phase 07 — Feedback Loop

## Current Position

Phase: 8
Plan: Not started

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
| Phase 03-llm-enrichment P02 | 2 | 2 tasks | 4 files |
| Phase 04-embeddings P01 | 2 | 1 tasks | 3 files |
| Phase 04-embeddings P02 | 2 | 2 tasks | 3 files |
| Phase 05-hard-filters P01 | 3 | 2 tasks | 3 files |
| Phase 06-scoring-engine P01 | 2 | 2 tasks | 2 files |
| Phase 06-scoring-engine P02 | 3 | 2 tasks | 3 files |
| Phase 07-feedback-loop P01 | 200 | 2 tasks | 4 files |

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
- [Phase 03-llm-enrichment]: commit after each successful job write (not batched) — partial batch progress is preserved on failure
- [Phase 03-llm-enrichment]: enriched_at IS NULL is the sole worker gate — upsert.py CASE expression clears it on content_hash change
- [Phase 03-llm-enrichment]: per-job failure isolation: classify_job exception increments failed counter without aborting the batch
- [Phase 04-embeddings]: Use retry_if_exception(_should_retry_openai) not retry_if_exception_type — restricts retries to 429 and 5xx only, 4xx pass through immediately
- [Phase 04-embeddings]: embed_text accepts optional client param — test-injectable without needing lru_cache invalidation tricks
- [Phase 04-embeddings]: register_vector(conn) in embed_pending — per-connection registration belongs in the function that writes vector columns
- [Phase 04-embeddings]: embed_pending mirrors enrich_pending exactly — same query/loop/commit/counters pattern for consistency
- [Phase 05-hard-filters]: Filter chain order: job_type -> sponsorship -> location; post-ANN retrieval (not SQL pre-filters)
- [Phase 05-hard-filters]: LOCATION_ALIASES canonical buckets: SF, NYC, LA, Remote, Seattle, Austin, Boston, Chicago with common variants
- [Phase 05-hard-filters]: remote-is-universal: location_raw=Remote matches any preference; preferred=Remote matches all jobs
- [Phase 06-scoring-engine]: score_location_fit reuses _job_location_buckets/_preferred_buckets from filters.py — no location normalization duplication
- [Phase 06-scoring-engine]: feedback_boost cold-start default is 0.5 per MTCH-13 — neutral signal for unknown companies
- [Phase 06-scoring-engine]: scorer.py signal functions are pure: only primitive inputs, no DB handles or ORM models passed in
- [Phase 06-scoring-engine]: ANN candidate limit is top_n * 4 — balances recall vs Python-side filter cost
- [Phase 06-scoring-engine]: affinity_embedding bypass: skip embed_text entirely when Phase 7 affinity vector is present — zero OpenAI calls per get_matches when affinity exists
- [Phase 06-scoring-engine]: conn injection pattern: if conn is not None use it, else get_connection() — matches embed_text client injection for test-injectability
- [Phase 07-feedback-loop]: ON CONFLICT DO NOTHING on feedback INSERT — idempotent re-recording without raising (ix_feedback_user_job index)
- [Phase 07-feedback-loop]: 70/30 affinity blend: 0.7 * existing + 0.3 * new_signal, normalize with 1e-9 epsilon; first like sets affinity directly (OpenAI embeddings are already unit-norm)
- [Phase 07-feedback-loop]: register_vector patched in tests — matches matcher.py test pattern; production code calls register_vector(conn) correctly

### Pending Todos

None yet.

### Blockers/Concerns

- Phase 2: Reconcile whether to parse `listings.json` (structured JSON) vs raw README markdown — if listings.json is available it eliminates the HTML parsing complexity entirely. Inspect the SimplifyJobs repo before building the parser.
- Phase 3: Enrichment prompt structure for Claude Haiku on terse listings (company name + role title only) needs empirical tuning before bulk run. Test against 50-100 real listings first.

## Session Continuity

Last session: 2026-03-26T03:18:21.211Z
Stopped at: Completed 07-feedback-loop-01-PLAN.md
Resume file: None
