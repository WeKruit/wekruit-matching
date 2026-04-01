# Requirements: WeKruit Matching Engine

**Defined:** 2026-03-31  
**Current milestone:** v1.2 Job Data Pipeline  
**Archived milestone:** v1.1 shipped — see [.planning/milestones/v1.1-REQUIREMENTS.md](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/milestones/v1.1-REQUIREMENTS.md)

## v1.2 Requirements — Job Data Pipeline

### Pipeline Infrastructure

- [x] **PIPE2-01**: The system tracks JD fetch attempts and sources per job so previously-attempted fetches are never re-run on subsequent cron cycles, preventing Firecrawl credit re-spend.
- [x] **PIPE2-02**: An alembic migration adds `jd_fetch_source`, `jd_fetch_attempted_at`, and `ats_content_hash` columns with a partial index that the enrichment queue query uses directly.
- [x] **PIPE2-03**: The daily pipeline executes ATS JD enrichment as Stage 2b between the existing JobRight enricher (Stage 2a) and LLM metadata classifier (Stage 2c), with zero changes to `cron_scraper.sh` or `enrich_from_jobright.py`.

### Source Parsers

- [x] **PARSE-01**: The system fetches full job description HTML from Greenhouse board API (`?content=true`) and returns a normalized plain-text description with salary, department, and location fields mapped to the canonical JD schema.
- [x] **PARSE-02**: The system fetches structured job data from Lever Postings API and maps `lists` (requirements, responsibilities, benefits), `descriptionPlain`, `salaryRange`, and `workplaceType` to the canonical JD schema.
- [x] **PARSE-03**: The system fetches Ashby job postings with `includeCompensation=true` and maps compensation, employment type, and description to the canonical JD schema.
- [x] **PARSE-04**: The system fetches Workday job descriptions via the CXS POST API (`/wday/cxs/{tenant}/{site}/jobs`) with a two-step tenant/site discovery step and Firecrawl fallback for Cloudflare-protected tenants.
- [x] **PARSE-05**: All ATS-sourced text passes through a normalization step (html.unescape + NFKC unicode normalization + zero-width character strip) before being written to the database.

### URL Resolution

- [x] **RESOLVE-01**: The URL classifier routes each job URL to the correct fetch tier (Greenhouse / Lever / Ashby free API, Workday CXS, or Firecrawl) using regex matching with no I/O.
- [x] **RESOLVE-02**: The system uses Firecrawl `/scrape` in markdown mode (1 credit) as the first-pass strategy for Workday and unknown career pages, escalating to Firecrawl `/extract` (5 credits) only when the heuristic detects no JD content in the scraped markdown.
- [x] **RESOLVE-03**: The system uses Firecrawl `/search` to discover employer ATS URLs when a SimplifyJobs URL is missing, broken, or redirects to a job aggregator.
- [x] **RESOLVE-04**: All Firecrawl calls are wrapped with an asyncio-level timeout (independent of the SDK `timeout` parameter) to prevent indefinite hangs caused by the known SDK timeout unit bug.

### Enrichment

- [x] **ENRICH-01**: Fetched `description_plain` is stored per job and passed to the existing LLM metadata classifier (SiliconFlow Qwen3-8B), replacing title-only input with full JD text for enrichment quality improvement.
- [x] **ENRICH-02**: Each job receives a `data_quality_score` (0-100: completeness 50 pts + recency 25 pts + description length 15 pts + salary presence 10 pts) computed at fetch time and stored for downstream filtering.

### Dashboard Observability

- [x] **DASH-01**: The pipeline page shows counts of jobs with and without JD text, segmented by ATS fetch source, so operators can see enrichment coverage at a glance.
- [x] **DASH-02**: The pipeline page exposes the JD enrichment queue depth (jobs with `jd_fetch_attempted_at IS NULL`) and the count of failed fetch attempts.
- [x] **DASH-03**: Operators receive an email digest after each enrichment run reporting jobs processed, credits consumed, failure counts by ATS type, and any Firecrawl errors.
- [x] **DASH-04**: The pipeline page shows the latest `data_quality_score` distribution (e.g., jobs scoring below 50) so operators can identify systematic enrichment gaps.

### Testing

- [x] **TEST-01**: The URL classifier has unit tests covering all ATS routing patterns including edge cases (subdomain variants, URL parameter variations, redirects).
- [x] **TEST-02**: End-to-end pipeline tests run against the latest 1K jobs (not the full 47K backfill) and assert that Greenhouse, Lever, and Ashby paths each produce at least one successfully enriched job with non-empty `description_plain`.

## Future Requirements

### External Surface

- **EXT-01**: External mode presents the jobs console with customer-facing copy, framing, and chrome distinct from internal mode.
- **EXT-02**: User can switch or route between internal and external console presentations without duplicating page logic.

### Pipeline Evolution

- **PIPE-EVO-01**: Tech stack extraction stored as a separate DB column for skills matching quality analysis.
- **PIPE-EVO-02**: Additional ATS platforms supported (SmartRecruiters, Jobvite, BambooHR, Rippling) after URL distribution analysis.
- **PIPE-EVO-03**: Salary filter enabled in matching engine once salary data coverage exceeds 30% of active jobs.
- **PIPE-EVO-04**: Ghost posting detection after 2-3 weeks of pipeline history establishes a no-change baseline.

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| PIPE2-01 | Phase 14 — DB Schema & URL Classifier | Complete |
| PIPE2-02 | Phase 14 — DB Schema & URL Classifier | Complete |
| PARSE-01 | Phase 15 — Free ATS Parsers | Complete |
| PARSE-02 | Phase 15 — Free ATS Parsers | Complete |
| PARSE-03 | Phase 15 — Free ATS Parsers | Complete |
| PARSE-04 | Phase 16 — URL Resolution & Firecrawl Integration | Complete |
| PARSE-05 | Phase 15 — Free ATS Parsers | Complete |
| RESOLVE-01 | Phase 14 — DB Schema & URL Classifier | Complete |
| RESOLVE-02 | Phase 16 — URL Resolution & Firecrawl Integration | Complete |
| RESOLVE-03 | Phase 16 — URL Resolution & Firecrawl Integration | Complete |
| RESOLVE-04 | Phase 16 — URL Resolution & Firecrawl Integration | Complete |
| ENRICH-01 | Phase 17 — Pipeline Orchestrator & Daily Integration | Complete |
| ENRICH-02 | Phase 15 — Free ATS Parsers | Complete |
| DASH-01 | Phase 18 — Observability, Email Digest & Testing | Complete |
| DASH-02 | Phase 18 — Observability, Email Digest & Testing | Complete |
| DASH-03 | Phase 18 — Observability, Email Digest & Testing | Complete |
| DASH-04 | Phase 18 — Observability, Email Digest & Testing | Complete |
| TEST-01 | Phase 14 — DB Schema & URL Classifier | Complete |
| TEST-02 | Phase 18 — Observability, Email Digest & Testing | Complete |
| PIPE2-03 | Phase 17 — Pipeline Orchestrator & Daily Integration | Complete |

---
*Last updated: 2026-03-31 after v1.2 JD pipeline completion*
