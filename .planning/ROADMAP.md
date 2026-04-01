# Roadmap: WeKruit Matching Engine

## Completed Milestones

- [x] **v1.1 Internal UI Foundation** — shared jobs console shell, responsive jobs browsing, and customer-facing-ready hierarchy. Archive: [.planning/milestones/v1.1-ROADMAP.md](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/milestones/v1.1-ROADMAP.md)
- [x] **v1.2 Job Data Pipeline** — ATS JD enrichment, Workday/Firecrawl resolution, Stage 2b orchestration, and operator observability. Audit: [.planning/v1.2-MILESTONE-AUDIT.md](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/v1.2-MILESTONE-AUDIT.md)

## Active Milestone

No active milestone is defined yet. v1.2 is complete and awaiting the next milestone definition.

## Phases

**Phase Numbering:**
- Integer phases (14, 15, 16, 17, 18): v1.2 milestone work
- Decimal phases (e.g., 14.1): urgent insertions if needed

### v1.2 — Job Data Pipeline

- [x] **Phase 14: DB Schema & URL Classifier** - Migration adding JD tracking columns and the URL router classifying each job URL to the correct ATS fetch tier
- [x] **Phase 15: Free ATS Parsers** - Greenhouse, Lever, and Ashby JSON API fetchers with HTML stripping, text normalization, and data quality scoring
- [x] **Phase 16: URL Resolution & Firecrawl Integration** - Workday CXS fetcher, Firecrawl scrape/extract/search chain with asyncio timeout safety, and full tiered routing
- [x] **Phase 17: Pipeline Orchestrator & Daily Integration** - `run_jd_enrichment.py` with batched queue processing, per-domain rate limiting, and Stage 2b insertion into daily.py
- [x] **Phase 18: Observability, Email Digest & Testing** - Pipeline dashboard enrichment stats, email digest after each run, data quality distribution view, and 1K-job end-to-end test suite

## Phase Details

### Phase 14: DB Schema & URL Classifier
**Goal**: The database is ready to track JD fetch attempts without corrupting existing content hashes, and every job URL can be routed to the correct ATS fetch tier before any network call is made
**Depends on**: Phase 11
**Requirements**: PIPE2-01, PIPE2-02, RESOLVE-01, TEST-01
**Success Criteria** (what must be TRUE):
  1. Alembic migration 0004 applies cleanly to staging and adds `jd_fetch_source`, `jd_fetch_attempted_at`, and `ats_content_hash` columns with the partial index on `(status, jd_fetch_attempted_at)` where `job_description IS NULL`
  2. A job URL for each of Greenhouse, Lever, Ashby, Workday, and an unknown domain is classified to the correct routing tier by `url_classifier.py` with no I/O
  3. Unit tests pass for all ATS URL patterns including subdomain variants, URL parameter variations, and known edge cases
  4. The existing `content_hash` column and `enrich_from_jobright.py` are unchanged and all existing pipeline tests continue to pass
**Plans**: TBD

### Phase 15: Free ATS Parsers
**Goal**: Greenhouse, Lever, and Ashby job descriptions are fetched at zero cost and stored as normalized plain text with canonical field mapping and a data quality score
**Depends on**: Phase 14
**Requirements**: PARSE-01, PARSE-02, PARSE-03, PARSE-05, ENRICH-02
**Success Criteria** (what must be TRUE):
  1. `ats_enricher.py` fetches a real Greenhouse job with `?content=true`, strips HTML from the `content` field, and stores a non-empty `description_plain` with salary, department, and location mapped to the canonical schema
  2. `ats_enricher.py` fetches a real Lever job and maps `lists` (requirements, responsibilities), `descriptionPlain`, `salaryRange`, and `workplaceType` to the canonical schema
  3. `ats_enricher.py` fetches a real Ashby job with `includeCompensation=true` and maps compensation and employment type to the canonical schema
  4. All fetched text passes through the normalization utility (html.unescape + NFKC + zero-width strip) and the normalized output contains no residual HTML tags or encoding artifacts
  5. Each enriched job receives a `data_quality_score` between 0 and 100 computed from completeness, recency, description length, and salary presence components
**Plans**: TBD

### Phase 16: URL Resolution & Firecrawl Integration
**Goal**: The long tail of Workday and unknown career pages can be fetched and their JD content extracted, with safe fallback chains and no risk of indefinite timeout hangs
**Depends on**: Phase 15
**Requirements**: PARSE-04, RESOLVE-02, RESOLVE-03, RESOLVE-04
**Success Criteria** (what must be TRUE):
  1. `firecrawl_enricher.py` fetches a real Workday job via the CXS POST API (`/wday/cxs/{tenant}/{site}/jobs`) after the two-step tenant/site discovery, and returns non-empty `description_plain`
  2. When the Workday CXS path is blocked by Cloudflare or returns empty content, the fetcher falls back to Firecrawl `/scrape` at 1 credit per page
  3. Firecrawl `/extract` at 5 credits is only invoked when the `_has_jd_content()` heuristic returns false on the `/scrape` markdown result
  4. All Firecrawl SDK calls are wrapped with an asyncio-level timeout that terminates the call within the configured window regardless of the SDK `timeout` parameter value
  5. When a SimplifyJobs URL is missing or redirects to a job aggregator, Firecrawl `/search` is invoked to discover the canonical employer ATS URL before attempting a fetch
**Plans**: TBD

### Phase 17: Pipeline Orchestrator & Daily Integration
**Goal**: All ATS enrichment components are wired into a single orchestrator that runs as Stage 2b in the daily pipeline, with batched commits, per-domain rate limiting, and zero disruption to the existing scraper
**Depends on**: Phase 16
**Requirements**: ENRICH-01, PIPE2-03
**Success Criteria** (what must be TRUE):
  1. `run_jd_enrichment.py` processes the enrichment queue in chunks of at most 500 rows per transaction, with `jd_fetch_attempted_at` and `jd_fetch_source` written on every attempt whether successful or not
  2. Per-domain rate limiting prevents sending more than the configured request rate to any single ATS domain within a rolling time window
  3. `daily.py` invokes Stage 2b (`run_jd_enrichment.py`) between Stage 2a (`enrich_from_jobright.py`) and Stage 2c (LLM metadata classifier) without any changes to `cron_scraper.sh`
  4. Fetched `description_plain` is passed as input to the LLM metadata classifier, replacing title-only enrichment input, and the classifier produces valid enriched output for at least one Stage 2b job
  5. A `--dry-run` flag runs the full orchestrator queue logic and routing decisions without writing to the database or consuming Firecrawl credits
**Plans**: TBD

### Phase 18: Observability, Email Digest & Testing
**Goal**: Operators can see JD enrichment coverage and quality at a glance on the pipeline page, receive an email summary after each run, and a 1K-job test suite confirms the full pipeline is working end-to-end
**Depends on**: Phase 17
**Requirements**: DASH-01, DASH-02, DASH-03, DASH-04, TEST-02
**Success Criteria** (what must be TRUE):
  1. The pipeline page shows a table of jobs-with-JD vs. jobs-without-JD segmented by `jd_fetch_source` (greenhouse, lever, ashby, workday, firecrawl, failed, null)
  2. The pipeline page shows the current enrichment queue depth (jobs with `jd_fetch_attempted_at IS NULL`) and the count of jobs with `jd_fetch_source = 'failed'`
  3. An email digest is sent after each enrichment run with jobs processed, Firecrawl credits consumed, failure counts by ATS type, and any error messages
  4. The pipeline page includes a `data_quality_score` distribution showing counts of jobs below 50 so operators can identify systematic enrichment gaps
  5. End-to-end tests run against the latest 1K jobs and assert that at least one job each from Greenhouse, Lever, and Ashby exits the pipeline with non-empty `description_plain` and a valid `data_quality_score`
**Plans**: TBD

## Progress

**Execution Order:**
- Completed backend foundation milestone: 1 → 8
- Completed UI milestone: 9 → 10 → 11
- Current pipeline milestone: 14 → 15 → 16 → 17 → 18

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 14. DB Schema & URL Classifier | 1/1 | Complete | 2026-03-31 |
| 15. Free ATS Parsers | 1/1 | Complete | 2026-03-31 |
| 16. URL Resolution & Firecrawl Integration | 1/1 | Complete | 2026-03-31 |
| 17. Pipeline Orchestrator & Daily Integration | 1/1 | Complete | 2026-03-31 |
| 18. Observability, Email Digest & Testing | 1/1 | Complete | 2026-03-31 |
