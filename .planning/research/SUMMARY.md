# Project Research Summary

**Project:** WeKruit Matching Engine
**Domain:** Python job scraping + vector matching engine (backend API, no frontend)
**Researched:** 2026-03-25
**Confidence:** HIGH

## Executive Summary

The WeKruit Matching Engine is a backend-only Python pipeline that scrapes internship and new-grad job listings from the SimplifyJobs GitHub repository, enriches them with LLM-derived metadata, generates semantic embeddings, and ranks them against user profiles via a weighted multi-signal scoring formula. The industry-standard architecture for this problem is a three-layer pipeline: an ingestion layer (scraper + enrichment worker), a storage layer (PostgreSQL + pgvector), and a matching layer (hard filters + ANN retrieval + weighted scoring). No open-source project combines all of these components in a single coherent backend — this engine addresses exactly that gap.

The recommended approach keeps the data model simple: a single PostgreSQL instance with the pgvector extension stores both structured job metadata and 1536-dimensional embeddings from OpenAI's `text-embedding-3-small`. The scraper, enrichment worker, and matching engine are strict pipeline stages with no circular dependencies. The matching function is exposed as a Python library entry point (`from matching.api import match`), not an HTTP service, per the spec's no-frontend constraint. Callers (Discord bot, web app) import directly or wrap in their own HTTP layer. LLM enrichment cost is the central operational concern: a content-hash gate that skips re-enrichment of unchanged jobs is non-negotiable from day one, not a later optimization.

The three critical risks are: (1) LLM enrichment cost explosion if the hash-gate is absent or incorrect — this can reach $600/month from a simple logic error; (2) pgvector silently falling back to sequential scan when the wrong distance operator is used, producing correct results at catastrophically wrong latency; and (3) SimplifyJobs README parsing failures on HTML-embedded multi-location cells and company-name emoji variations, which corrupt stable IDs and cause duplicate enrichment. All three are preventable at build time if addressed in the correct phase.

---

## Key Findings

### Recommended Stack

The spec's technology choices are validated without exception. Python 3.12+, PostgreSQL 16+, pgvector, httpx, the Anthropic SDK, and the OpenAI SDK are all correct. The one addition the spec omits is the Postgres adapter: **psycopg3** (the `psycopg` package, not `psycopg2`) is the correct 2026 choice — 3-5x better memory efficiency, native async support, and explicitly recommended by pgvector's own documentation for new projects. The project should be initialized with `uv`, not pip or poetry — `uv` is the 2025/2026 standard for new Python projects, providing lock files, Python version pinning, and 10-100x faster installs.

**Core technologies:**
- Python 3.12+: runtime — stable LTS with performance gains over prior versions; spec constraint
- PostgreSQL 16+ + pgvector 0.8+: primary data store and vector index — eliminates a separate vector DB; single query combines structured filters and ANN
- psycopg3 (psycopg package): Postgres adapter — spec omits this; psycopg2 is maintenance-only; psycopg3 is the correct new-project choice
- httpx 0.28.1: HTTP client for GitHub scraping — sync+async in one library; replaces requests
- anthropic 0.86.0: LLM enrichment — structured classification of industry, skills, sponsorship
- openai 2.30.0: embedding generation — `text-embedding-3-small` at 1536 dims, best cost/quality ratio
- numpy 1.26+: vector arithmetic — sufficient for per-user weighted scoring without scipy overhead
- pydantic v2 + pydantic-settings: data validation and env config — Rust-backed, 5-50x faster than v1; pydantic-settings replaces python-dotenv
- alembic 1.18.x: schema migrations — autogenerate tracks schema diffs during development
- tenacity 8.x: retry with exponential backoff — required for all LLM API calls
- uv: package manager — replaces pip + venv + requirements.txt

**What not to use:** psycopg2 (maintenance-only), requests (no async/HTTP2), LangChain (loses cost control), dedicated vector DBs (Pinecone/Qdrant — unnecessary at this scale), IVFFlat pgvector index (HNSW has better recall/latency tradeoff for incremental inserts).

### Expected Features

The feature dependency graph is strictly ordered: scraper produces jobs, enrichment adds metadata and embeddings, matching consumes enriched jobs, and feedback consumes matching results. Nothing in the feedback layer can be built before the scoring pipeline is stable.

**Must have (table stakes) — v1:**
- SimplifyJobs scraper (`listings.json`, Summer2026-Internships + New-Grad-Positions repos) — the entire data source
- Stable ID generation (hash on normalized company + title + url) + upsert with staleness marking — without this the DB drifts from reality
- LLM enrichment with content-hash gate — metadata needed for scoring; hash check is cost control, not optional
- Embedding generation and pgvector storage — required for semantic signal in scoring
- Location normalization (alias map for top-20 US tech hubs + Remote variants) — location_fit signal is meaningless without it
- Fuzzy skill normalization — skills overlap has the highest weight (0.25)
- Hard filter enforcement (sponsorship, job type, location exclusions) — must precede scoring
- Weighted multi-signal scoring (title 0.30, skills 0.25, industry 0.15, company_size 0.10, location 0.10, recency 0.05, feedback_boost 0.05)
- Ranked results API with per-signal score breakdown — what callers consume
- Cron-ready CLI entrypoints — makes the system operational

**Should have (differentiators) — v1.x after validation:**
- Feedback loop (like/dislike) — requires at least one consumer sending real user interactions first
- User affinity embedding (rolling weighted average of liked job embeddings) — only meaningful once feedback accumulates
- Recency decay scoring (exponential decay, tunable lambda) — initial lambda may need adjustment based on observed result quality
- Sponsorship classification (LLM-derived `true/false/unknown`) — high value for visa-dependent users

**Defer (v2+):**
- Additional data sources beyond SimplifyJobs
- Skill gap recommendations
- Batch profile matching (N profiles x M jobs)
- A/B weight testing framework
- Resume parsing (caller's responsibility per spec)
- Collaborative filtering, salary normalization, web dashboard

### Architecture Approach

The architecture is a strict three-layer pipeline with no cross-layer coupling: the ingestion layer (scraper + enrichment worker) writes to PostgreSQL, and the matching layer reads from PostgreSQL. Components within each layer communicate only through the shared DB — the scraper never calls enrichment directly, and the matching engine never invokes scraping or enrichment APIs. The matching function is a Python library entry point, not an HTTP server. All shared data models (Job, UserProfile, MatchResult) live in a `models/` package to prevent circular imports across the three layers.

**Major components:**
1. **Scraper** (`scraper/`) — fetches GitHub raw README, parses markdown tables (including HTML-embedded multi-location cells), generates stable IDs, upserts to DB; outputs normalized job records; knows nothing about enrichment
2. **Enrichment Worker** (`enrichment/`) — reads unenriched jobs, calls Anthropic for classification and OpenAI for embeddings, writes enriched records; never touches user profiles; skips jobs whose content hash is unchanged
3. **DB Layer** (`db/`) — connection pool, query helpers, upsert logic; single source of truth; never invokes business logic
4. **Matching Engine** (`matching/`) — applies hard filters, runs pgvector ANN retrieval (top-100 candidates), applies 7-signal weighted scoring, returns ranked list; read-only against jobs table
5. **Feedback Handler** (`feedback/`) — records like/dislike signals, nudges user affinity embedding via lerp, writes to user_profiles and feedback tables only
6. **Models** (`models/`) — shared pydantic dataclasses for Job, UserProfile, MatchResult; imported by all components

**Key patterns:**
- Two-stage match: hard filter (SQL WHERE) → ANN retrieval (pgvector) → weighted score; hard filters reduce N=2000 to ~400, ANN narrows to 100, scoring ranks the shortlist
- Conditional enrichment: `WHERE enriched_at IS NULL` before any LLM call; hash-gate for re-enrichment
- Feedback embedding shift: lerp by 0.05 toward liked job embedding, away from disliked; small factor prevents drift

### Critical Pitfalls

1. **HTML in SimplifyJobs markdown table cells breaks naive parsers** — use `mistune` parser; post-process cells to strip HTML tags; track `↳` continuation rows to inherit parent company name; test against a live README snapshot that includes multi-location and continuation rows. If not caught in Phase 1, re-enrichment costs compound every duplicate record.

2. **LLM enrichment cost explosion on every scrape run** — store a content hash of enrichable fields; only call Anthropic when hash changes; use Claude Haiku (not Sonnet/Opus) for classification; use Batch API for 50% cost reduction on bulk runs. At 2,000 jobs and $0.01/job, a missing hash check costs $600/month.

3. **pgvector silently falls back to sequential scan** — standardize exclusively on cosine distance (`<=>`) with `vector_cosine_ops` index; never mix operators; apply hard SQL filters post-retrieval (not pre-), as pre-filters shrink the candidate set enough to trigger a planner-chosen sequential scan; verify index usage with `EXPLAIN ANALYZE` after every schema change.

4. **Stable ID breaks on company name emoji variations** — normalize before hashing: strip all emoji, lowercase, strip punctuation, collapse whitespace; use `(normalized_company, normalized_role, primary_location)` as composite key; test against rows with FAANG emoji, lock emoji, and `↳` continuations. ID errors require expensive deduplication and re-enrichment to recover from.

5. **GitHub rate limiting on unauthenticated raw.githubusercontent.com fetches** — always authenticate with a GitHub PAT in the `Authorization: Bearer` header; add explicit 429 detection and exponential backoff; never deploy unauthenticated in production (GitHub announced stricter limits in May 2025 explicitly targeting raw file downloads).

---

## Implications for Roadmap

Architecture research identifies a clear build order with hard dependencies. The phase structure below follows that order directly.

### Phase 1: Foundation and Scraper

**Rationale:** Everything depends on having job data in the database. The DB schema must exist before the scraper writes to it, and stable ID generation must be correct before enrichment runs — a bad ID function means paying double enrichment cost for every duplicate. This phase has the highest density of critical pitfalls (3 of 5 top pitfalls are Phase 1 concerns).

**Delivers:** PostgreSQL schema with pgvector extension, connection pool, migrations via alembic; SimplifyJobs scraper that correctly handles HTML-embedded multi-location cells, emoji normalization, and `↳` continuation rows; stable ID generation; upsert with staleness marking; cron-ready CLI entrypoint for scraping.

**Addresses features:** SimplifyJobs scraping, stable ID generation, upsert with staleness marking, cron-ready entrypoint

**Avoids pitfalls:**
- HTML in markdown cells (use mistune, test against live README snapshot)
- Stable ID breaking on emoji (normalize before hashing)
- Unauthenticated GitHub fetch rate limiting (PAT authentication from day one)

### Phase 2: Enrichment and Embeddings

**Rationale:** Enrichment depends on Phase 1 producing raw job records. The content-hash gate and embedding model tracking columns must be designed before running any enrichment at scale — retrofitting them after the first bulk run wastes money and produces inconsistent data. The pgvector index operator class must be set correctly from the first embedding insert.

**Delivers:** LLM enrichment pipeline (Anthropic Claude Haiku) for industry, company size, skills, and sponsorship classification; content-hash gate that skips unchanged jobs; OpenAI `text-embedding-3-small` embedding generation with batching; `embedding_model` metadata column; HNSW index on embedding column with `vector_cosine_ops`; cron-ready CLI entrypoint for enrichment.

**Addresses features:** LLM enrichment with incremental hash check, embedding generation and pgvector storage, sponsorship classification

**Uses stack:** anthropic SDK, openai SDK, pgvector, numpy, tenacity (retry), pydantic v2 for enrichment output validation

**Avoids pitfalls:**
- LLM enrichment cost explosion (content-hash gate, Haiku model, Batch API)
- pgvector sequential scan (HNSW with `vector_cosine_ops`, verified with `EXPLAIN ANALYZE`)
- Embedding drift (store `embedding_model` column from day one)
- LLM hallucination (confidence scores, null/unknown sponsorship state, controlled skills vocabulary)

### Phase 3: Matching Engine

**Rationale:** Matching requires enriched jobs with embeddings from Phase 2. Location normalization and fuzzy skill matching must be built as part of the matching engine — they are scoring inputs, not scraper outputs. Hard filters must be applied post-retrieval (after pgvector ANN), not as SQL pre-filters, to preserve index usage.

**Delivers:** Two-stage matching pipeline (hard filters → pgvector ANN top-100 → 7-signal weighted scoring); location normalization (alias map); fuzzy skill normalization; ranked results API with per-signal score breakdown; `match(user_profile) -> list[MatchResult]` library entry point callable by consumers.

**Addresses features:** Hard filter enforcement, location normalization, fuzzy skill matching, weighted multi-signal scoring, ranked results API with signal breakdown

**Avoids pitfalls:**
- pgvector pre-filter sequential scan (apply hard filters post-ANN retrieval)
- Cold start poor matches (detect new-user state; suppress feedback_boost; return deliberately broad top-K on first query)

### Phase 4: Feedback Loop

**Rationale:** Feedback requires the matching engine to be stable and producing results that consumers can react to. Diversity injection and feedback decay must be designed from the start of this phase — they are much harder to add after users have accumulated feedback histories.

**Delivers:** Like/dislike feedback recording; affinity embedding shift (lerp by 0.05); feedback decay (time-weighted; recent signals outweigh old ones); diversity injection (at least 20% of top-K results outside established affinity cluster); user profile update pipeline.

**Addresses features:** Feedback loop (like/dislike), user affinity embedding, recency decay scoring

**Avoids pitfalls:**
- Feedback filter bubble (diversity injection and feedback decay designed from day one)

### Phase 5: Integration, Testing, and Cron Wiring

**Rationale:** End-to-end validation closes all the "looks done but isn't" gaps identified in PITFALLS.md. Cron scheduling should use standalone scripts + system cron in production, not APScheduler in-process, per the spec's "cron-ready scripts" framing.

**Delivers:** Full end-to-end pipeline smoke test (scrape → enrich → match → feedback cycle); all "looks done but isn't" checklist items verified; cron wiring (system cron or APScheduler for local dev); security review (no PAT in source, parameterized queries, rate-limit awareness on embedding API calls).

**Addresses features:** Cron-ready entrypoints (both scraper and enrichment)

### Phase Ordering Rationale

- Phase 1 before Phase 2: enrichment requires raw job records; ID correctness prevents double enrichment cost
- Phase 2 before Phase 3: matching requires embeddings; pgvector index must exist before matching queries run
- Phase 3 before Phase 4: feedback requires users to have seen match results; affinity embedding update requires job embeddings to exist
- Phase 4 before Phase 5: integration tests require all pipeline components to exist
- Location normalization and fuzzy skill matching are in Phase 3 (not Phase 1) because they are matching inputs, not storage inputs — job location is stored raw and normalized at query time

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 2:** LLM enrichment prompt engineering — the exact prompt structure for Claude Haiku to produce structured JSON (industry, company size, skills list, sponsorship) with confidence scores needs empirical tuning; hallucination rate on terse job listings is uncertain
- **Phase 4:** Feedback decay function and diversity injection threshold — the lerp factor (0.05) and diversity floor (20%) are reasonable starting values from research but need calibration against real user behavior; no strong prior for this specific domain

Phases with standard patterns (skip research-phase):
- **Phase 1:** SimplifyJobs scraping is well-documented; the parsing challenges are identified and solutions are known (mistune, emoji normalization, continuation row tracking)
- **Phase 3:** Multi-signal weighted scoring is well-documented in recommender systems literature; weights (title 0.30, skills 0.25, etc.) are grounded in feature research
- **Phase 5:** Standard integration testing and cron wiring patterns; no domain-specific unknowns

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All choices verified against PyPI, official docs, and multiple independent sources; psycopg3 recommendation verified directly from psycopg.org and pgvector-python docs |
| Features | HIGH (core), MEDIUM (differentiators) | Core pipeline features verified against SimplifyJobs repo inspection and competitor analysis; feedback and affinity embedding design based on industry patterns, not prior WeKruit implementation |
| Architecture | HIGH | Build order verified against hard dependency analysis; component boundaries validated against multiple production pipeline architecture references; pgvector patterns verified against official pgvector docs and Crunchy Data benchmarks |
| Pitfalls | HIGH | Primary sources for all critical pitfalls: GitHub Changelog (rate limits), pgvector GitHub issues (index fallback), SimplifyJobs live README inspection (HTML cells, emoji), Anthropic docs (structured outputs limitations) |

**Overall confidence:** HIGH

### Gaps to Address

- **Enrichment prompt tuning:** The exact prompt structure for extracting structured metadata from terse SimplifyJobs listings (often just company name + role title) needs empirical testing; confidence scores requested from Claude may be poorly calibrated on short inputs. Address in Phase 2 with a batch of 50-100 real listings before running at scale.
- **Weight calibration:** The multi-signal weights (title 0.30, skills 0.25, etc.) are grounded in feature research but have not been validated against real WeKruit user behavior. Treat as v1 defaults; plan to adjust based on observed match quality once a consumer is live.
- **SimplifyJobs `listings.json` vs README:** FEATURES.md notes that `listings.json` is the correct data source (not raw README markdown). ARCHITECTURE.md shows the README as the source. Reconcile during Phase 1 implementation — if `listings.json` is available as a structured JSON feed, it eliminates the markdown parsing complexity from Pitfall 1 entirely. Inspect the repo before building the parser.
- **Feedback loop timing:** FEATURES.md gates the feedback loop on "at least one consumer sending real user interactions." This creates a dependency on a downstream consumer being live. Ensure the matching engine is designed to handle zero feedback gracefully (cold-start path in Phase 3) so the feedback phase can be deferred without breaking Phase 3 functionality.

---

## Sources

### Primary (HIGH confidence)
- PyPI: anthropic 0.86.0, openai 2.30.0, httpx 0.28.1, pgvector 0.4.2, alembic 1.18.x — version and compatibility confirmed
- psycopg.org/psycopg3 — "if you are starting a new project, you should probably start from psycopg3"
- github.com/pgvector/pgvector-python — confirmed psycopg3 + SQLAlchemy 2.x compatibility
- GitHub Changelog: "Updated rate limits for unauthenticated requests" (May 8, 2025) — https://github.blog/changelog/2025-05-08-updated-rate-limits-for-unauthenticated-requests/
- pgvector GitHub issues #835, #72 — HNSW index fallback behavior and cosine distance semantics
- SimplifyJobs Summer2026-Internships README (live inspection) — confirmed `listings.json` format, HTML cells, emoji usage
- Crunchydata HNSW blog — HNSW preferred over IVFFlat for recall/latency tradeoff
- Anthropic Docs: Structured outputs — format compliance vs factual accuracy distinction
- OpenAI Docs: text-embedding-3-small specs — 1536 dims, 8192 token limit, batch API

### Secondary (MEDIUM confidence)
- Eightfold AI engineering blog — multi-signal scoring: skill overlap, career trajectory, company similarity, recency
- rabiuk/job-scraper (open-source) — reference SimplifyJobs scraper architecture for deduplication pattern
- Crunchy Data: pgvector performance for developers — `maintenance_work_mem` and HNSW configuration
- Multiple sources (BetterStack, Speakeasy, Oxylabs) — httpx vs requests vs aiohttp comparison
- Multiple sources (Medium, Upsun, DataCamp) — uv vs pip vs poetry 2025

### Tertiary (LOW confidence, needs validation)
- Feedback lerp factor (0.05) and diversity injection floor (20%) — reasonable starting values from recommender systems patterns but not validated for this specific domain
- Enrichment cost estimate ($0.01/job) — order-of-magnitude correct but depends on actual prompt token count and Haiku pricing at time of implementation

---
*Research completed: 2026-03-25*
*Ready for roadmap: yes*
