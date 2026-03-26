# Roadmap: WeKruit Matching Engine

## Overview

Build a production-ready Python backend pipeline that pulls job listings from SimplifyJobs GitHub repos, enriches them with LLM-derived metadata and semantic embeddings, and returns ranked matches against user profiles via a weighted multi-signal scoring formula. The pipeline is delivered as a Python library — no HTTP server — so any caller (Discord bot, web app) can import and run it directly. Eight phases build the system bottom-up: database foundation, scraper, LLM enrichment, embedding generation, hard filter layer, scoring engine, feedback loop, and final integration wiring.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Foundation** - Project scaffolding, Postgres + pgvector schema, migrations, and environment config (completed 2026-03-26)
- [ ] **Phase 2: Scraper** - GitHub README fetch, markdown parsing, stable ID generation, and upsert pipeline
- [ ] **Phase 3: LLM Enrichment** - Anthropic classification of industry, skills, company size, and sponsorship with cost controls
- [ ] **Phase 4: Embeddings** - OpenAI text-embedding-3-small generation, pgvector storage, and HNSW index
- [ ] **Phase 5: Hard Filters** - Job type, sponsorship, and location pre-filtering with normalization
- [ ] **Phase 6: Scoring Engine** - 7-signal weighted scoring, ranked results API, and cold-start handling
- [ ] **Phase 7: Feedback Loop** - Like/dislike recording, affinity embedding updates, and preference propagation
- [ ] **Phase 8: Integration & Operations** - End-to-end test, cron wiring, library packaging, and environment documentation

## Phase Details

### Phase 1: Foundation
**Goal**: The project is runnable and the database is ready to receive job data
**Depends on**: Nothing (first phase)
**Requirements**: FOUND-01, FOUND-02, FOUND-03, FOUND-04, FOUND-05, FOUND-06, FOUND-07, FOUND-08
**Success Criteria** (what must be TRUE):
  1. Running `uv sync` installs all dependencies and `python -c "import wekruit_matching"` succeeds
  2. Running the migration command creates the jobs, user_profiles, and feedback tables with the vector(1536) column and HNSW index present
  3. A test insert into the jobs table succeeds and EXPLAIN ANALYZE on a cosine similarity query shows index scan (not sequential scan)
  4. All configuration is read from a `.env` file via pydantic-settings — the app raises a clear error if required vars are missing
**Plans**: 2 plans

Plans:
- [x] 01-01-PLAN.md — Project scaffold, uv setup, pydantic models, and pydantic-settings config
- [x] 01-02-PLAN.md — psycopg3 connection pool, SQLAlchemy table definitions, alembic migrations, HNSW index

### Phase 2: Scraper
**Goal**: Job listings are fetched from both SimplifyJobs repos, parsed correctly, and persisted to the database with stable IDs
**Depends on**: Phase 1
**Requirements**: SCRP-01, SCRP-02, SCRP-03, SCRP-04, SCRP-05, SCRP-06, SCRP-07, SCRP-08, SCRP-09
**Success Criteria** (what must be TRUE):
  1. Running the scraper populates the jobs table with listings from both Summer2026-Internships and New-Grad-Positions repos
  2. Closed listings (lock emoji rows) are not inserted — querying the jobs table for closed rows returns zero results
  3. Re-running the scraper on unchanged data produces zero new inserts and zero content hash changes
  4. Jobs that disappeared from the README on a second scrape are marked inactive (not deleted)
  5. Running the scraper against a README snapshot containing HTML-embedded cells, continuation rows, and emoji company names produces correct stable IDs with no duplicates
**Plans**: 3 plans

Plans:
- [x] 02-01-PLAN.md — GitHub authenticated fetcher (httpx + PAT) and stable ID/content hash utilities
- [ ] 02-02-PLAN.md — README parser: HTML cell stripping, lock row filtering, continuation row inheritance
- [ ] 02-03-PLAN.md — Upsert pipeline (ON CONFLICT DO UPDATE, stale marking) and scraper orchestrator

### Phase 3: LLM Enrichment
**Goal**: Every unenriched job in the database is classified with industry, company size, required skills, and sponsorship likelihood — without re-enriching unchanged jobs
**Depends on**: Phase 2
**Requirements**: ENRC-01, ENRC-02, ENRC-03, ENRC-04, ENRC-05, ENRC-09, ENRC-10
**Success Criteria** (what must be TRUE):
  1. Running the enrichment worker classifies all unenriched jobs with industry, company_size, skills, and sponsorship fields populated
  2. Re-running the enrichment worker on jobs with unchanged content hashes makes zero Anthropic API calls
  3. Enrichment output contains no hallucinated values — skills lists and industry values are drawn from a controlled vocabulary; unknown/null is a valid first-class output
  4. All Anthropic API calls retry with exponential backoff on 429/5xx responses; a single API failure does not abort the entire enrichment run
**Plans**: TBD

### Phase 4: Embeddings
**Goal**: Every enriched job has a semantic embedding stored in pgvector, with model provenance tracked for future drift detection
**Depends on**: Phase 3
**Requirements**: ENRC-06, ENRC-07, ENRC-08
**Success Criteria** (what must be TRUE):
  1. Running the embedding step populates the embedding column for all enriched jobs
  2. Every embedding row has a non-null embedding_model value (e.g., "text-embedding-3-small")
  3. A pgvector cosine similarity query against the jobs table returns results in ranked order and EXPLAIN ANALYZE confirms the HNSW index is used
  4. Re-running the embedding step on jobs with unchanged content hashes makes zero OpenAI API calls
**Plans**: TBD

### Phase 5: Hard Filters
**Goal**: Callers can constrain matches to specific job types, sponsorship requirements, and locations before scoring runs
**Depends on**: Phase 4
**Requirements**: MTCH-01, MTCH-02, MTCH-03
**Success Criteria** (what must be TRUE):
  1. Passing `job_type="intern"` in a profile returns only internship listings; passing `job_type="new_grad"` returns only new grad listings
  2. Passing `requires_sponsorship=True` in a profile excludes all jobs where sponsorship is classified as False or unknown
  3. Passing `location="SF"` in a profile matches jobs tagged "San Francisco", "San Francisco, CA", and "SF, CA" — the alias map normalizes them to the same bucket
**Plans**: TBD

### Phase 6: Scoring Engine
**Goal**: Users can call `get_matches(profile, top_n=30)` and receive a ranked list of jobs with per-signal score breakdowns
**Depends on**: Phase 5
**Requirements**: MTCH-04, MTCH-05, MTCH-06, MTCH-07, MTCH-08, MTCH-09, MTCH-10, MTCH-11, MTCH-12, MTCH-13
**Success Criteria** (what must be TRUE):
  1. Calling `get_matches(profile, top_n=30)` returns a list of up to 30 job dicts, each with a `score` and a `signals` breakdown showing individual component scores
  2. Changing a profile's skills list visibly changes ranking — jobs matching the new skills rank higher
  3. A profile with no feedback history (cold-start) receives results without errors — the feedback_boost signal is neutral (0) and other signals drive ranking
  4. The scoring function applies title similarity at weight 0.30, skills overlap at 0.25, industry match at 0.15, company size at 0.10, location fit at 0.10, recency at 0.05, and feedback boost at 0.05 — weights sum to 1.00
**Plans**: TBD
**UI hint**: no

### Phase 7: Feedback Loop
**Goal**: Users can record reactions to job matches and those reactions measurably shift future match rankings
**Depends on**: Phase 6
**Requirements**: FDBK-01, FDBK-02, FDBK-03, FDBK-04, FDBK-05
**Success Criteria** (what must be TRUE):
  1. Calling the feedback function with a like reaction inserts a record in the feedback table and adds the job's company to the user's liked_companies list
  2. Calling the feedback function with a dislike reaction inserts a record in the feedback table and adds the job's company to the user's disliked_companies list
  3. After liking 3 jobs from the same industry, that industry ranks higher in subsequent `get_matches` results for that user compared to a fresh profile with identical explicit preferences
  4. The user's affinity embedding updates after each like — calling `get_matches` after a like returns a different (shifted) ranking compared to before the like
**Plans**: TBD

### Phase 8: Integration & Operations
**Goal**: The full pipeline runs end-to-end, can be scheduled via cron, and is importable as a Python library by any consumer
**Depends on**: Phase 7
**Requirements**: INTG-01, INTG-02, INTG-03, INTG-04, INTG-05
**Success Criteria** (what must be TRUE):
  1. Running the end-to-end test script completes the full pipeline — scrape, enrich, embed, match, feedback — against real SimplifyJobs data and prints ranked results without manual intervention
  2. The cron scripts for scraper (6 AM ET) and enrichment (6:30 AM ET) can be installed with a single `crontab -e` entry and run without errors on subsequent triggers
  3. `from wekruit_matching import get_matches, record_feedback` works in a fresh Python environment with only the package installed — no HTTP server required
  4. `.env.example` documents every required environment variable with a description — a developer can set up the project from zero using only that file and the README
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation | 2/2 | Complete   | 2026-03-26 |
| 2. Scraper | 1/3 | In Progress|  |
| 3. LLM Enrichment | 0/? | Not started | - |
| 4. Embeddings | 0/? | Not started | - |
| 5. Hard Filters | 0/? | Not started | - |
| 6. Scoring Engine | 0/? | Not started | - |
| 7. Feedback Loop | 0/? | Not started | - |
| 8. Integration & Operations | 0/? | Not started | - |
