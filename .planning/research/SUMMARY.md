# Project Research Summary

**Project:** WeKruit Matching Engine — Job Data Collection Pipeline (Milestone: Firecrawl + ATS Scraping)
**Domain:** Python job scraping pipeline with tiered ATS fetching, structured JD extraction, and vector matching
**Researched:** 2026-03-31 (updated; original 2026-03-25)
**Confidence:** HIGH

## Executive Summary

The WeKruit Matching Engine has a working foundation from the original milestone (2026-03-25): GitHub README scraping from SimplifyJobs repos, LLM enrichment via SiliconFlow Qwen3-8B, OpenAI text-embedding-3-small embeddings, and 7-signal weighted matching against user profiles. The new milestone replaces title-only LLM enrichment with full job description text by following employer ATS URLs from the SimplifyJobs data. The correct approach is a tiered fetcher routing system: Greenhouse and Lever have free unauthenticated JSON APIs that return full JD content (zero cost, use httpx directly); Workday exposes an undocumented-but-stable CXS POST API for most tenants; and Firecrawl cloud handles the long tail of custom, JS-heavy, or bot-protected career pages. ATS URL classification happens before any fetch and determines which path is invoked — a router pattern, not optional middleware. The key cost control is to exhaust the free API paths first (covering ~55% of URLs) and use Firecrawl's scrape endpoint (1 credit) rather than its extract endpoint (5 credits) wherever possible, routing returned markdown into the existing LLM pipeline.

The primary architectural decision is to add the new enrichment as completely separate modules — `url_classifier.py`, `ats_enricher.py`, `firecrawl_enricher.py`, and `run_jd_enrichment.py` — inserted as Stage 2b in the existing `daily.py` pipeline between the JobRight enricher (Stage 2a) and the LLM metadata classifier (Stage 2c). Two new DB columns (`jd_fetch_source`, `jd_fetch_attempted_at`) gate the incremental queue and prevent re-spending Firecrawl credits on already-attempted jobs. The existing `enrich_from_jobright.py` is preserved unchanged; the new stage targets a mutually exclusive WHERE clause (`primary_url NOT LIKE 'jobright.ai/%'`). The HNSW index pitfall — confirmed 100x slowdown on non-vector column updates per pgvector GitHub issue #875 — requires keeping JD text updates and embedding inserts as separate pipeline stages, which the proposed architecture already satisfies.

The three critical risks for this milestone are: (1) Firecrawl's SDK has a known timeout unit bug (GitHub issue #1848) that passes milliseconds where the underlying HTTP library expects seconds, causing workers to hang indefinitely — always apply an asyncio-level timeout wrapper independent of the SDK parameter; (2) Supabase's authenticated role has an 8-second statement timeout that kills batches larger than ~500 rows — the pipeline already hit this in production on a 33K-row batch; (3) the existing `content_hash` gate must not be modified to include ATS-derived fields because the first ATS scrape would change every row's hash and trigger a full re-enrichment of 76K jobs — a separate `ats_content_hash` column is required. The initial ~23K-job backfill should use self-hosted Firecrawl via Docker Compose (no credit cost) rather than the $16/month cloud plan, then switch to the cloud for ongoing incremental daily work (50-200 new jobs/day, well within the Hobby plan).

## Key Findings

### Recommended Stack

The base stack from the original milestone is unchanged and confirmed: Python 3.12+, PostgreSQL 16+ with pgvector 0.8+, psycopg3, httpx, anthropic SDK, openai SDK, numpy, pydantic v2, alembic, tenacity, loguru, uv. The new milestone adds three libraries and one external service:

**Core technologies:**
- Python 3.12 + httpx 0.28.1: direct ATS JSON API calls (Greenhouse, Lever, Ashby) — free, synchronous, no scraping overhead; existing pattern from `enrich_from_jobright.py`
- firecrawl-py 4.21.0 + Firecrawl Cloud Hobby ($16/month): JS-heavy ATS pages (Workday, iCIMS, custom career sites) — cloud required for proxy rotation and anti-bot protection; self-hosted lacks Fire-engine; use self-hosted only for the initial bulk backfill
- beautifulsoup4 4.14.x + lxml 5.x: strip HTML from Greenhouse `content` and Workday `description` API response fields before passing to LLM — do not add Playwright or a full browser stack just to strip HTML tags
- SiliconFlow Qwen3-8B (free tier): existing LLM enricher; now receives richer input (full JD text instead of title only); the 50 req/day free-tier cap without purchased credits forces batching 5 jobs/request; purchasing $10 in credits unlocks 1,000 req/day
- PostgreSQL 16 + pgvector 0.8+, psycopg3: unchanged; two new columns added via alembic migration 0004 (`jd_fetch_source TEXT`, `jd_fetch_attempted_at TIMESTAMPTZ`)

**Critical version note:** `firecrawl-py` has a known SDK timeout unit bug — always wrap calls with an asyncio-level timeout independent of the SDK's `timeout` parameter.

**What not to add:** Playwright (only if Workday CXS API fails for a named tenant — defer by default; 200MB browser binary); Scrapy/crawlee (full crawl framework overhead for targeted ATS endpoint calls); Firecrawl for Greenhouse or Lever (both have free public JSON APIs — Firecrawl credits are wasted here).

See `.planning/research/STACK.md` for full version matrix, ATS API endpoint specifications, and Firecrawl pricing tiers.

### Expected Features

Feature research covers the new data collection pipeline milestone. Original milestone features (matching engine core) remain in force from the prior SUMMARY.md and are not repeated here.

**Must have (table stakes):**
- ATS platform detection from URL — routes each job to the correct fetch strategy (free API vs. Firecrawl scrape vs. Workday POST); everything downstream depends on this routing decision
- Greenhouse API fetch (`?content=true`) — largest share of SimplifyJobs listings; free, zero credits; returns `title`, `location`, `content` (HTML JD), `departments`, salary via `?pay_transparency=true`
- Lever API fetch — second most common ATS; native structured `lists` field separates requirements, responsibilities, and benefits as named arrays; `descriptionPlain` already stripped of HTML
- Firecrawl `/scrape` in markdown mode for Workday + unknown platforms — covers the long tail at 1 credit/page
- Incremental `content_hash` check before fetch — gates the expensive structured extraction step; prevents re-fetching unchanged JDs
- Canonical JD schema mapping (ATS native fields to canonical schema) — unifies output across all ATS platforms into a single record shape
- `description_plain` stored and passed to existing LLM enrichment pipeline — immediate, concrete improvement to enrichment quality
- Retry with tenacity (exponential backoff, 3 attempts, 2-30s window) on fetch failures
- `data_quality_score` computation (0-100: completeness 50pts + recency 25pts + description length 15pts + salary presence 10pts) — enables downstream filtering

**Should have (competitive differentiators):**
- Firecrawl JSON mode structured extraction — extract `required_skills`, `preferred_skills`, `tech_stack`, `seniority_level`, `salary_min/max`, `remote_policy`, `visa_sponsorship` as typed fields from Workday/custom pages; costs 5 credits/page; only for pages where the API does not return structured fields natively
- Salary normalization and `salary_confidence` flag (exact/extracted/inferred/missing) — Ashby has the best native salary coverage; Lever has `salaryRange`; Greenhouse has `pay_input_ranges` (state disclosure required)
- Ashby API fetch (`includeCompensation=true`) — growing ATS adoption at Series A/B startups; better salary coverage than any other ATS
- Visa sponsorship re-classification using full JD text — materially more accurate than title-only inference; patterns for "unable to sponsor", "H-1B", "OPT/CPT eligible" work best on full description
- Firecrawl `/search` for employer URL discovery — when SimplifyJobs URL is broken/missing/redirecting to aggregator

**Defer (v2+):**
- Tech stack extraction as a separate DB column — defer until skills matching quality is validated with `description_plain`
- Additional ATS platforms (SmartRecruiters, Jobvite, BambooHR, Rippling) — defer until URL distribution analysis shows a gap
- Salary filter in matching engine — defer until salary data coverage exceeds 30% of active jobs
- Ghost posting detection — requires 2-3 weeks of pipeline history to establish a "no change" baseline (multiple scrape cycles)

See `.planning/research/FEATURES.md` for the full canonical JD schema, data quality scoring formula, feature dependency graph, and competitor comparison.

### Architecture Approach

The new pipeline inserts as Stage 2b between the existing JobRight enrichment (Stage 2a) and the LLM metadata classifier (Stage 2c) in `daily.py`. It is additive — zero changes to `enrich_from_jobright.py`, `enrichment/worker.py`, or `embedding/worker.py`. The two enrichment stages use mutually exclusive DB queries: Stage 2a targets `primary_url LIKE 'jobright.ai/%'`; Stage 2b targets `primary_url NOT LIKE 'jobright.ai/%' AND jd_fetch_attempted_at IS NULL`. No job can be processed by both stages. Tiered routing routes each URL to the cheapest method that can succeed: Tier 0 (JobRight, $0) → Tier 1 (Greenhouse/Lever/Ashby free APIs, $0) → Tier 2 (Firecrawl /scrape, 1 credit) → Tier 3 (Firecrawl /extract, 5 credits). For the initial 23K-job backfill, use self-hosted Firecrawl (Docker Compose, no credit cost); for daily incremental work (50-200 jobs/day), use Firecrawl Cloud Hobby ($16/month).

**Major components:**
1. `url_classifier.py` — classify URL into routing tier via regex; pure string matching, no I/O; independently unit-testable before any DB or network work begins
2. `ats_enricher.py` — Greenhouse, Lever, and Ashby JSON API fetchers; free path; uses httpx (existing); strips HTML via bs4/lxml; applies text normalization (html.unescape + NFKC + zero-width strip) before writing to DB
3. `firecrawl_enricher.py` — Firecrawl /scrape first (1 credit); escalates to /extract (5 credits) only when `_has_jd_content()` heuristic returns false; handles Workday CXS POST API with Firecrawl fallback for Cloudflare-protected tenants
4. `run_jd_enrichment.py` — orchestrator; queries the enrichment queue; dispatches to ats_enricher or firecrawl_enricher per URL tier; writes `jd_fetch_source` + `jd_fetch_attempted_at` on every attempt (success or failure); enforces 500-row batch commit chunks; applies per-domain rate limiting
5. DB migration 0004 — adds `jd_fetch_source TEXT` and `jd_fetch_attempted_at TIMESTAMPTZ` with a partial index on `(status, jd_fetch_attempted_at) WHERE job_description IS NULL AND primary_url IS NOT NULL`

**Build order is strict:** DB migration → url_classifier → ats_enricher → firecrawl_enricher → run_jd_enrichment → daily.py modification → embedding text enrichment (optional, last).

See `.planning/research/ARCHITECTURE.md` for the full system diagram, all 5 architecture patterns with code examples, credit budget breakdown, and anti-patterns.

### Critical Pitfalls

1. **Firecrawl SDK timeout unit bug (Pitfall A1)** — SDK `timeout` parameter is passed as seconds to the underlying HTTP library, not milliseconds as documented; `timeout=60000` blocks for 16.6 hours. Always wrap Firecrawl calls with `asyncio.wait_for()` at the application level independent of the SDK. Set `ExitTimeout` in launchd plist. Reference: firecrawl/firecrawl GitHub issue #1848.

2. **No attempt-tracking column re-spends credits on every cron run (Architecture Anti-Pattern 2)** — Without `jd_fetch_attempted_at`, a failed fetch (404, empty markdown, JS wall) re-spends Firecrawl credits on every daily run indefinitely. Write `jd_fetch_attempted_at = NOW()` and `jd_fetch_source = 'failed'` on every attempt, successful or not. The queue query filters `WHERE jd_fetch_attempted_at IS NULL`.

3. **Supabase 8-second statement timeout kills batches larger than ~500 rows (Pitfall A7)** — The pipeline already hit this in production on a 33K-row batch. Cap all upsert batches at 500 rows with commit after each chunk. For initial bulk backfill, use direct Postgres connection (port 5432 session mode) with `SET statement_timeout = 0` — the Supabase pooler ignores per-session timeout overrides.

4. **HNSW index degrades 100x for non-vector column updates (Pitfall A10)** — Updating non-vector columns (`job_description`, `enriched_at`, `status`) on the HNSW-indexed jobs table is ~100x slower than without the index — confirmed pgvector GitHub issue #875. Never update `job_description` in the same transaction that touches the `embedding` column. The proposed architecture already separates Stage 2b (JD text updates) from Stage 3 (embedding inserts).

5. **ATS `content_hash` collision triggers full 76K-job re-enrichment (Pitfall A9)** — Adding `job_description` to the existing `content_hash` input changes every row's hash on first ATS scrape, triggering a full re-enrichment run. Add a separate `ats_content_hash` column (SHA-256 of `description_plain`) for tracking ATS-derived field changes. Never modify the existing `content_hash` logic.

6. **New ATS module imports break the working GitHub scraper (Pitfall A12)** — Extending the existing scraper module with Firecrawl code risks breaking the working pipeline via import errors or connection pool conflicts when `firecrawl-py` is added. Implement ATS scraping as a completely separate script with its own entrypoint and launchd plist. The existing `cron_scraper.sh` must not be modified.

## Implications for Roadmap

The build order is strict and flows from component dependencies. Each phase produces independently runnable, tested components before the next phase builds on them.

### Phase 1: DB Schema + URL Classifier Foundation

**Rationale:** Zero risk and zero external dependencies. The DB migration must exist before any code can write the new columns. The URL classifier is pure string matching with no I/O — it can be built and unit-tested before any HTTP calls are made. Both are hard prerequisites for every subsequent phase.
**Delivers:** Alembic migration 0004 (`jd_fetch_source`, `jd_fetch_attempted_at` columns + partial index); `url_classifier.py` with full ATS routing table (greenhouse.io, lever.co, ashbyhq.com, myworkdayjobs patterns); unit test coverage for all URL patterns including edge cases
**Addresses:** ATS platform detection (table stakes), incremental hash check gate infrastructure
**Avoids:** Credit re-spend on previously-attempted jobs (Pitfall A anti-pattern 2); `ats_content_hash` vs `content_hash` collision (Pitfall A9) — add `ats_content_hash` column in this migration

### Phase 2: Free ATS JSON API Fetchers (Greenhouse + Lever + Ashby)

**Rationale:** Validates the ATS enrichment concept for $0 before any Firecrawl credits are spent. Greenhouse and Lever together cover the majority of SimplifyJobs URLs. This phase proves out the canonical JD schema mapping against real production data and establishes the text normalization utility that all subsequent phases must use.
**Delivers:** `ats_enricher.py` with Greenhouse, Lever, and Ashby fetchers; bs4/lxml HTML stripping from `content`/`description` fields; text normalization utility (html.unescape + NFKC normalization + zero-width character strip); data quality score computation; canonical JD schema mapping for ATS native fields
**Uses:** httpx (existing), beautifulsoup4 + lxml (new), psycopg3 (existing)
**Implements:** ATS Enricher component (free tier of the routing table)
**Avoids:** Encoding contamination of embedding input (Pitfall A13 — normalization utility built here becomes a required step for all subsequent ATS parsers); wasting Firecrawl credits on APIs that have free JSON endpoints

### Phase 3: Firecrawl Integration (Workday + Unknown Career Pages)

**Rationale:** Only after Phase 2 is validated with real production data should Firecrawl be introduced. By this point the canonical schema mapping is proven, the orchestration pattern is established, and the DB tracking columns exist. Self-host Firecrawl via Docker Compose for the initial 23K-job backfill to avoid ~$80 in cloud credits; switch to Firecrawl Cloud Hobby for ongoing incremental work.
**Delivers:** `firecrawl_enricher.py` with scrape-first (1 credit) / extract-fallback (5 credits) chain; application-level asyncio timeout wrapper; `_has_jd_content()` heuristic; Workday CXS POST API path (httpx) with Firecrawl fallback for Cloudflare-protected tenants; Firecrawl `/search` URL discovery for missing/broken URLs; FIRECRAWL_API_KEY config field with graceful degradation when absent
**Uses:** firecrawl-py 4.21.0
**Implements:** Tiered Fetcher Routing (Pattern 1), Fallback Chain Within Firecrawl Tier (Pattern 3), Search Discovery (Pattern 4)
**Avoids:** SDK timeout hang (Pitfall A1 — asyncio wrapper required); 5x credit multiplier from using extract everywhere (Pitfall A2 — scrape-first strategy); batch job stuck indefinitely (Pitfall A3 — job ID persistence + 90-min max age); Workday server suffix hardcoding (Pitfall A4 — two-step CXS endpoint discovery); Cloudflare blocking direct CXS calls (Pitfall A5 — Firecrawl fallback for enterprise Workday tenants)

### Phase 4: Pipeline Orchestrator + Daily Integration

**Rationale:** Final integration happens only after all fetcher components are independently tested. The orchestrator (`run_jd_enrichment.py`) wires everything together with per-domain throttling, error isolation, and chunked batch commits. The `daily.py` modification is the last change made — it is the highest-risk edit to the existing working pipeline and should be last.
**Delivers:** `run_jd_enrichment.py` orchestrator with chunked batch processing (500 rows/transaction hard limit); per-domain rate limiting via `defaultdict(deque)` tracking last-access-time per domain; credit budget controls; updated `daily.py` inserting Stage 2b between Stage 2a and Stage 2c; `--dry-run` flag tested before enabling on production cron
**Implements:** Credit-Aware Batch Processing with DB Tracking (Pattern 2)
**Avoids:** Supabase statement timeout (Pitfall A7 — 500-row batch cap enforced in orchestrator); HNSW slowdown (Pitfall A10 — JD update stage inherently separate from embedding stage); module coupling breaking GitHub scraper (Pitfall A12 — separate module, separate entrypoint, `cron_scraper.sh` untouched)

### Phase 5: Embedding Quality Improvement (Optional Enhancement)

**Rationale:** Once the pipeline has been running for 1-2 weeks and `job_description` is populated for a significant share of jobs, re-embedding with richer text input (title + company + skills + `job_description[:500]`) materially improves semantic match quality. Deferred until data quality can be validated and re-embedding cost estimated against actual token counts.
**Delivers:** Updated `embedding/worker.py` text construction; re-embedding of jobs whose `job_description` changed since last embed; cost estimate validation before triggering bulk re-embed run
**Avoids:** Embedding drift (Pitfall B6 — per-row `embedding_model` tracking already exists); unnecessary bulk re-embedding cost before data quality is confirmed

### Phase Ordering Rationale

- Foundation first: DB migration must precede all code that writes new columns; URL classifier must precede all fetchers that route by it.
- Free paths before paid paths: ATS JSON APIs (Phase 2) validate the enrichment approach at zero cost and with official API documentation before committing to Firecrawl spend.
- Isolated components before orchestration: build and test each fetcher in isolation (Phases 2-3) before the orchestrator (Phase 4) combines them — this isolates bugs to the component level.
- Daily pipeline modification last: the existing working pipeline is the highest-value asset in the codebase; it should only be modified after all new components are independently validated.
- This ordering exactly mirrors the ARCHITECTURE.md build order (Steps 1-7).

### Research Flags

Phases needing deeper attention during planning:
- **Phase 3 (Firecrawl / Workday):** Workday CXS API is MEDIUM confidence — undocumented, community-sourced. Test against at least 3 real Workday tenants (mix of enterprise and startup) before committing to the direct httpx approach. Budget Firecrawl fallback for enterprise Workday pages protected by Cloudflare Bot Management.
- **Phase 3 (Firecrawl / Batch):** Firecrawl async batch job reliability is a known production issue (Pitfall A3). Design for the stuck-job case from day one — job ID persistence + 90-minute max-age timeout are mandatory, not optional.
- **Phase 5 (Embeddings):** Measure re-embedding token cost before running against the full corpus — `job_description[:500]` adds ~125 tokens per job; confirm total cost against actual `description_plain` length distribution before triggering a bulk re-embed.

Phases with standard patterns (skip additional research):
- **Phase 1 (DB Migration + URL Classifier):** Alembic migration is well-documented; URL classifier is pure regex with no novel decisions.
- **Phase 2 (ATS JSON APIs):** Greenhouse and Lever APIs have official documentation with HIGH confidence across all fields; Ashby is similarly well-documented.
- **Phase 4 (Orchestrator):** Pattern established by the existing `enrich_from_jobright.py` and `pipeline/daily.py`; follow existing conventions.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Base stack unchanged and confirmed. firecrawl-py, bs4, lxml verified against PyPI and official docs. SiliconFlow rate limits are MEDIUM — official docs confirm 1,000 RPM/50K TPM but the 50 req/day free-tier cap was sourced from rate-limits page + community sources |
| Features | HIGH | Greenhouse, Lever, Ashby APIs verified against official documentation. Workday CXS API is MEDIUM (community-sourced, no official docs). Canonical JD schema and data quality scoring formula are well-reasoned heuristics — weights are MEDIUM confidence and should be tuned against real data |
| Architecture | HIGH | Component boundaries, build order, and integration points derived from direct codebase analysis + official docs. Credit budget estimate (~9K credits for 23K jobs) is MEDIUM — URL distribution by ATS type is estimated from simplifyJobs patterns, not measured from actual `listings.json` |
| Pitfalls | HIGH | A1 (SDK timeout bug) sourced from firecrawl GitHub issue #1848. A7 (Supabase timeout) confirmed by direct WeKruit production experience. A10 (HNSW non-vector slowdown) sourced from pgvector GitHub issue #875. Most new-milestone pitfalls from official issue trackers, not community speculation |

**Overall confidence:** HIGH

### Gaps to Address

- **Workday CXS API `wd_server` suffix variability:** The `wd{N}` server suffix is not derivable without fetching the company's career page first. The two-step discovery pattern is specified in ARCHITECTURE.md but needs validation against a representative sample of actual SimplifyJobs employer URLs before scaling.
- **SiliconFlow 50 req/day free-tier cap:** At 200+ jobs/day enrichment volume, batching 5 jobs per LLM request must be implemented and tested before production runs. Alternatively, $10 in credits unlocks 1,000 req/day. Validate batching behavior and output quality (multi-job structured JSON extraction) before enabling.
- **Firecrawl backfill cost:** Self-hosting Firecrawl for the initial 23K-job backfill requires Docker Compose setup on the matching server. Validate the self-hosted setup works (Redis + Postgres stack) and produces equivalent output quality before starting the backfill run.
- **Ashby URL share in SimplifyJobs data:** Research notes Ashby adoption is growing but doesn't quantify what percentage of SimplifyJobs URLs are Ashby. Measure this from actual `listings.json` URL distribution before committing to Phase 2 implementation scope.
- **`__NEXT_DATA__` migration risk (Pitfall A6):** The existing `enrich_from_jobright.py` uses `__NEXT_DATA__` extraction. If JobRight migrates from Next.js Pages Router to App Router, this will break silently. Add a presence assertion and fallback path — identified as a Phase 2/3 boundary risk.

## Sources

### Primary (HIGH confidence)
- Greenhouse Job Board API — `boards-api.greenhouse.io/v1/boards/{token}/jobs`; all query params and field names; https://developers.greenhouse.io/job-board.html
- Lever postings-api GitHub README — full field listing including salaryRange, lists, workplaceType; https://github.com/lever/postings-api/blob/master/README.md
- Ashby Job Postings API docs — endpoint URL, compensation fields, `includeCompensation` param; https://developers.ashbyhq.com/docs/public-job-posting-api
- Firecrawl scrape/extract/search/batch endpoint docs — verified endpoints, credit costs, SDK usage; https://docs.firecrawl.dev
- Firecrawl self-hosting guide — confirms Fire-engine (proxy/stealth) absent in self-hosted; https://docs.firecrawl.dev/contributing/self-host
- Firecrawl pricing — 500 free lifetime credits, Hobby $16/3K credits/month, Standard $83/100K; https://www.firecrawl.dev/pricing
- Firecrawl rate limits — Hobby: 100 RPM scrape, 50 RPM search, 15 RPM crawl; https://docs.firecrawl.dev/rate-limits
- pgvector GitHub issue #875 — HNSW non-vector update performance degradation (100x); confirmed issue
- firecrawl/firecrawl GitHub issue #1848 — SDK timeout unit bug
- SiliconFlow rate limits and pricing — Qwen3-8B $0.06/M tokens; 1,000 RPM / 50K TPM; 50 req/day without credits; https://www.siliconflow.com/models/qwen3-8b
- Existing codebase analysis — `enrich_from_jobright.py`, `pipeline/daily.py`, `db/tables.py`, `config.py`; HIGH confidence from direct code reading

### Secondary (MEDIUM confidence)
- Workday CXS API pattern (`POST /wday/cxs/{tenant}/{site}/jobs`) — Apify actors + GitHub community crawlers (chuchro3/WebCrawler, blackfalcondata/workday-scraper); no official docs
- OpenRouter free tier (20 RPM / 200 req/day, 29 free models) — https://openrouter.ai/collections/free-models; useful fallback if SiliconFlow proves unreliable
- Firecrawl caching `maxAge` parameter — changelog; pricing pages change frequently, treat with caution
- Data quality scoring formula — adapted from Clarity Scorecard completeness × recency methodology; weights are reasonable starting points, not empirically validated for job postings

### Tertiary (LOW confidence — validate before relying on)
- Workday `wd_server` suffix universality — community observation from scrapers; may vary more than documented; validate against real employer URLs before scaling
- Ghost posting rate estimate (30-40%) — cited from job data normalization blog; not independently verified for the SimplifyJobs intern/new grad corpus specifically

---
*Research completed: 2026-03-31*
*Ready for roadmap: yes*
