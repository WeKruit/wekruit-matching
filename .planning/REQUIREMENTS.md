# Requirements: WeKruit Platform

**Defined:** 2026-04-01
**Current milestone:** v2.0 Platform Unification
**Previous milestones:** v1.2 (JD Pipeline), v1.1 (Internal UI)

---

## v2.0 Requirements — Platform Unification

### User Sync

- [x] **SYNC-01**: Cloud Function receives Supabase DB Webhook payload (INSERT/UPDATE on users table) and validates the request signature
- [x] **SYNC-02**: Webhook receiver maps VALET user fields (skills, preferences, workAuth, resumeSummary) to PlatformUser schema and writes to Firestore `/platform-users/{uid}`
- [x] **SYNC-03**: Supabase Database Webhook is configured on the users and resumes tables to POST to the Firebase Cloud Function endpoint on INSERT and UPDATE events
- [x] **SYNC-04**: User sync completes in < 1 second end-to-end with retry handling, deduplication, and sync event logging

### Job Sync

- [x] **JSYNC-01**: Cloud Function POST `/api/sync/jobs` receives batched job payloads (up to 500 per request) and upserts to Firestore `/matching-jobs/{jobId}` using content_hash diffing
- [x] **JSYNC-02**: Python sync script runs after daily pipeline and POSTs new/changed active embedded jobs to the Firebase sync endpoint in batches
- [ ] **JSYNC-03**: One-time bulk load script syncs all ~47K active jobs (with 1536-dim embeddings) from Postgres to Firestore
- [x] **JSYNC-04**: Pipeline marks jobs inactive in Firestore when they become stale in Postgres (status sync)

### Matching

- [x] **MATCH-01**: Matching Cloud Function applies hard filters via Firestore WHERE clauses (sponsorship, industry, recency, location) before any vector computation, reducing candidates to ~500 docs
- [ ] **MATCH-02**: In-memory cosine similarity computes distance between user query embedding and ~500 filtered job embeddings in TypeScript (< 50ms)
- [x] **MATCH-03**: 7-signal weighted scorer is ported from Python to TypeScript with identical weights and logic (title_similarity, skills_overlap, industry_match, company_size_match, location_fit, recency, feedback_boost)
- [x] **MATCH-04**: User can like/dislike/apply to jobs and bookmark them; feedback persists in Firestore and influences the feedback_boost signal in subsequent matches

### Job Board API

- [x] **BOARD-01**: GET `/api/matching/jobs` returns paginated job listings with status, industry, location, and sponsorship filters
- [ ] **BOARD-02**: Search and advanced filters allow querying by keyword (company, title), skills, salary range, and seniority level with Firestore composite indexes
- [x] **BOARD-03**: GET `/api/matching/jobs/:id` returns full job detail including JD text, skills, salary, apply link, qualifications, and responsibilities
- [x] **BOARD-04**: Handoff doc is expanded with current pipeline architecture (launchd plists, scripts, log paths), Mac Mini setup instructions, Firecrawl Docker setup, and complete infrastructure map

### v2.0 Future Requirements

- Firebase Auth as single identity provider (replace VALET custom JWT)
- Job board frontend (Next.js on Firebase Hosting)
- Real-time job alerts (Firestore onSnapshot or push notifications)
- Resume-to-profile auto-mapping (parse resume → populate preferences)
- Analytics dashboard (match quality, engagement rates, popular skills)

### v2.0 Out of Scope

- VALET code changes (sync via DB webhooks, zero modifications)
- Frontend UI (API-only in this milestone)
- Matching algorithm improvements (port as-is, optimize later)
- Mobile app
- Payment/billing integration

### v2.0 Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| BOARD-04 | Phase 19 — Handoff & Infrastructure Doc | Complete |
| SYNC-01 | Phase 20 — User Sync Cloud Function | Implemented |
| SYNC-02 | Phase 20 — User Sync Cloud Function | Implemented |
| SYNC-03 | Phase 20 — User Sync Cloud Function | Complete |
| SYNC-04 | Phase 20 — User Sync Cloud Function | Implemented |
| JSYNC-01 | Phase 21 — Job Sync Pipeline | Implemented |
| JSYNC-02 | Phase 21 — Job Sync Pipeline | Implemented |
| JSYNC-03 | Phase 21 — Job Sync Pipeline | Pending |
| JSYNC-04 | Phase 21 — Job Sync Pipeline | Implemented |
| MATCH-01 | Phase 22 — Matching Cloud Function | Implemented |
| MATCH-02 | Phase 22 — Matching Cloud Function | Pending |
| MATCH-03 | Phase 22 — Matching Cloud Function | Implemented |
| MATCH-04 | Phase 22 — Matching Cloud Function | Implemented |
| BOARD-01 | Phase 23 — Job Board API | Implemented |
| BOARD-02 | Phase 23 — Job Board API | Pending |
| BOARD-03 | Phase 23 — Job Board API | Implemented |

---

## v1.2 Requirements — Job Data Pipeline (Completed)

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
| BOARD-04 | Phase 19 — Handoff & Infrastructure Doc | Complete |
| SYNC-01 | Phase 20 — User Sync Cloud Function | Implemented |
| SYNC-02 | Phase 20 — User Sync Cloud Function | Implemented |
| SYNC-03 | Phase 20 — User Sync Cloud Function | Complete |
| SYNC-04 | Phase 20 — User Sync Cloud Function | Implemented |
| JSYNC-01 | Phase 21 — Job Sync Pipeline | Implemented |
| JSYNC-02 | Phase 21 — Job Sync Pipeline | Implemented |
| JSYNC-03 | Phase 21 — Job Sync Pipeline | Pending |
| JSYNC-04 | Phase 21 — Job Sync Pipeline | Implemented |
| MATCH-01 | Phase 22 — Matching Cloud Function | Implemented |
| MATCH-02 | Phase 22 — Matching Cloud Function | Pending |
| MATCH-03 | Phase 22 — Matching Cloud Function | Implemented |
| MATCH-04 | Phase 22 — Matching Cloud Function | Implemented |
| BOARD-01 | Phase 23 — Job Board API | Implemented |
| BOARD-02 | Phase 23 — Job Board API | Pending |
| BOARD-03 | Phase 23 — Job Board API | Implemented |

---
*Last updated: 2026-04-02 after production webhook cutover and direct matching E2E validation*
