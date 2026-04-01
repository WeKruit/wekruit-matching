# Roadmap: WeKruit Matching Engine

## Milestones

- ✅ **v1.1 Internal UI Foundation** — Phases 9-11 (shipped 2026-03-31)
- ✅ **v1.2 Job Data Pipeline** — Phases 14-18 (shipped 2026-03-31)
- 🚧 **v2.0 Platform Unification** — Phases 19-23 (in progress)

## Phases

<details>
<summary>✅ v1.2 Job Data Pipeline (Phases 14-18) - SHIPPED 2026-03-31</summary>

- [x] **Phase 14: DB Schema & URL Classifier** - Migration adding JD tracking columns and the URL router classifying each job URL to the correct ATS fetch tier
- [x] **Phase 15: Free ATS Parsers** - Greenhouse, Lever, and Ashby JSON API fetchers with HTML stripping, text normalization, and data quality scoring
- [x] **Phase 16: URL Resolution & Firecrawl Integration** - Workday CXS fetcher, Firecrawl scrape/extract/search chain with asyncio timeout safety, and full tiered routing
- [x] **Phase 17: Pipeline Orchestrator & Daily Integration** - `run_jd_enrichment.py` with batched queue processing, per-domain rate limiting, and Stage 2b insertion into daily.py
- [x] **Phase 18: Observability, Email Digest & Testing** - Pipeline dashboard enrichment stats, email digest after each run, data quality distribution view, and 1K-job end-to-end test suite

</details>

### v2.0 Platform Unification (In Progress)

**Milestone Goal:** Firebase Core Service becomes the central hub for all customer-facing APIs — user sync from VALET Supabase, job sync from the Mac Mini pipeline, matching engine on Cloud Functions, and a browsable job board API. One user registration propagates across the entire WeKruit system.

- [ ] **Phase 19: Handoff & Infrastructure Doc** - Comprehensive doc covering pipeline architecture, Mac Mini setup, launchd plists, Firecrawl Docker, and collection naming conventions
- [ ] **Phase 20: User Sync Cloud Function** - Supabase DB Webhook receiver maps VALET user fields to PlatformUser schema and writes to Firestore `/platform-users/{uid}` in under 1 second
- [ ] **Phase 21: Job Sync Pipeline** - Python sync script and one-time bulk loader sync active embedded jobs from Postgres to Firestore `/matching-jobs/{jobId}` with content_hash diffing
- [ ] **Phase 22: Matching Cloud Function** - Filter-first Firestore WHERE queries reduce candidates to ~500 docs, then in-memory cosine sim and 7-signal scorer run in TypeScript
- [ ] **Phase 23: Job Board API** - Paginated browse, keyword search, composite filters, and full job detail endpoint on Cloud Functions

## Phase Details

### Phase 19: Handoff & Infrastructure Doc
**Goal**: All service boundaries, naming conventions, Mac Mini setup steps, and pipeline architecture are documented so any engineer can reason about the full system without tribal knowledge
**Depends on**: Phase 18
**Requirements**: BOARD-04
**Success Criteria** (what must be TRUE):
  1. The handoff doc contains a complete infrastructure map showing all services (Mac Mini pipeline, Firebase Cloud Functions, Supabase, Firecrawl Docker) with their roles and communication paths
  2. A new engineer can follow the Mac Mini setup section to configure launchd plists, log paths, and the daily cron script without asking anyone
  3. Firecrawl Docker setup instructions cover pulling images, running the 5 containers, configuring port 3002, and verifying the service is reachable
  4. Collection prefix naming conventions (`platform-`, `matching-`, `outbound-`) are documented with the service ownership rationale for each
  5. Pipeline architecture section covers all pipeline stages (scrape, enrich, embed, sync) with script names, input/output, and the data flow between Postgres and Firestore
**Plans**: TBD

### Phase 20: User Sync Cloud Function
**Goal**: A new VALET user registration propagates to Firestore within 1 second via a Supabase DB Webhook, with zero changes to VALET code
**Depends on**: Phase 19
**Requirements**: SYNC-01, SYNC-02, SYNC-03, SYNC-04
**Success Criteria** (what must be TRUE):
  1. A Supabase DB Webhook fires on INSERT and UPDATE to the users table and delivers the payload to the Firebase Cloud Function endpoint within 1 second
  2. The Cloud Function validates the Supabase webhook signature and rejects requests with invalid or missing signatures with a 401
  3. A VALET user's skills, preferences, workAuth, and resumeSummary fields are mapped to the PlatformUser schema and written to Firestore `/platform-users/{uid}` after a successful webhook delivery
  4. Duplicate webhook deliveries for the same event are deduplicated and do not create duplicate Firestore writes
  5. Sync events are logged with enough detail (uid, event type, timestamp, success/failure) to diagnose delivery failures after the fact
**Plans**: TBD

### Phase 21: Job Sync Pipeline
**Goal**: All ~47K active embedded jobs are loaded into Firestore and the daily pipeline keeps Firestore in sync with Postgres using content_hash diffing, with no full re-syncs required after the initial load
**Depends on**: Phase 19
**Requirements**: JSYNC-01, JSYNC-02, JSYNC-03, JSYNC-04
**Success Criteria** (what must be TRUE):
  1. The one-time bulk load script writes all ~47K active embedded jobs (including 1536-dim embedding arrays) from Postgres to Firestore `/matching-jobs/{jobId}` without hitting Firestore write limits
  2. The Cloud Function POST `/api/sync/jobs` accepts batches of up to 500 jobs and upserts only records whose content_hash has changed, skipping unchanged records
  3. After the daily pipeline run, the Python sync script identifies new and changed active jobs and POSTs them to the Firebase sync endpoint in batches without re-syncing the full corpus
  4. When a job becomes stale in Postgres, the sync marks its Firestore document inactive rather than deleting it, preserving referential integrity for saved/bookmarked jobs
**Plans**: TBD

### Phase 22: Matching Cloud Function
**Goal**: Users get personalized job matches with filter-first Firestore queries cutting candidates to ~500 docs before any vector computation, with the 7-signal scorer fully ported to TypeScript
**Depends on**: Phase 21
**Requirements**: MATCH-01, MATCH-02, MATCH-03, MATCH-04
**Success Criteria** (what must be TRUE):
  1. A match request with sponsorship, industry, recency, or location filters results in a Firestore WHERE query that eliminates non-matching jobs before any cosine computation runs
  2. In-memory cosine similarity over the ~500 filtered job embeddings completes in under 50ms in the Cloud Function runtime
  3. The 7-signal scorer (title_similarity, skills_overlap, industry_match, company_size_match, location_fit, recency, feedback_boost) produces the same ranked output as the Python scorer for identical inputs
  4. A user can like, dislike, apply to, or bookmark a job and that feedback persists in Firestore and raises or lowers that job's feedback_boost signal in subsequent match requests for that user
**Plans**: TBD

### Phase 23: Job Board API
**Goal**: Callers can browse, search, filter, and retrieve full job details through a stable Cloud Functions API backed by Firestore composite indexes
**Depends on**: Phase 22
**Requirements**: BOARD-01, BOARD-02, BOARD-03
**Success Criteria** (what must be TRUE):
  1. GET `/api/matching/jobs` returns a paginated list of jobs that can be filtered by status, industry, location, and sponsorship using Firestore composite indexes
  2. A keyword search on company name or job title returns matching jobs without a full table scan, using Firestore array-contains or composite index queries
  3. Advanced filters for skills, salary range, and seniority level are queryable and backed by Firestore composite indexes that do not hit the per-collection index limit
  4. GET `/api/matching/jobs/:id` returns the full job record including JD text, skills array, salary, apply link, qualifications, and responsibilities in a single Firestore read
**Plans**: TBD

## Progress

**Execution Order:**
- Completed backend foundation: 1 → 8
- Completed UI milestone: 9 → 10 → 11
- Completed pipeline milestone: 14 → 15 → 16 → 17 → 18
- Current platform unification: 19 → 20 → 21 → 22 → 23 (Phase 20 and 21 are independent after Phase 19)

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 14. DB Schema & URL Classifier | v1.2 | 1/1 | Complete | 2026-03-31 |
| 15. Free ATS Parsers | v1.2 | 1/1 | Complete | 2026-03-31 |
| 16. URL Resolution & Firecrawl Integration | v1.2 | 1/1 | Complete | 2026-03-31 |
| 17. Pipeline Orchestrator & Daily Integration | v1.2 | 1/1 | Complete | 2026-03-31 |
| 18. Observability, Email Digest & Testing | v1.2 | 1/1 | Complete | 2026-03-31 |
| 19. Handoff & Infrastructure Doc | v2.0 | 0/1 | Not started | - |
| 20. User Sync Cloud Function | v2.0 | 0/1 | Not started | - |
| 21. Job Sync Pipeline | v2.0 | 0/1 | Not started | - |
| 22. Matching Cloud Function | v2.0 | 0/1 | Not started | - |
| 23. Job Board API | v2.0 | 0/1 | Not started | - |
