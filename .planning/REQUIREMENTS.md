# Requirements: WeKruit Matching Engine

**Defined:** 2026-03-25
**Core Value:** Given a user profile, return the most relevant job listings ranked by fit

## v1 Requirements

### Foundation

- [x] **FOUND-01**: Project uses Python 3.12+ with uv for package management
- [x] **FOUND-02**: Postgres database with pgvector extension is configured and accessible
- [x] **FOUND-03**: Database schema created with jobs, user_profiles, and feedback tables
- [x] **FOUND-04**: Jobs table includes vector(1536) column with HNSW index for cosine similarity
- [x] **FOUND-05**: Pydantic v2 models validate all data structures at boundaries
- [x] **FOUND-06**: Database connection pool via psycopg3 (async-capable)
- [x] **FOUND-07**: Alembic migrations manage schema changes
- [x] **FOUND-08**: Environment config via pydantic-settings (.env support)

### Scraper

- [x] **SCRP-01**: Scraper fetches raw README from SimplifyJobs/Summer2026-Internships GitHub repo
- [x] **SCRP-02**: Scraper fetches raw README from SimplifyJobs/New-Grad-Positions GitHub repo
- [x] **SCRP-03**: Parser correctly handles embedded HTML in markdown table cells (details/summary blocks)
- [x] **SCRP-04**: Parser skips closed listings (lock emoji rows)
- [x] **SCRP-05**: Parser handles continuation rows (arrow emoji for same-company listings)
- [x] **SCRP-06**: Stable ID generation with emoji normalization (strip decorative emoji before hashing)
- [x] **SCRP-07**: GitHub fetch uses authenticated requests (PAT) to avoid rate limits
- [x] **SCRP-08**: Upsert logic: insert new jobs, update existing, mark stale as inactive
- [x] **SCRP-09**: Content hash per job to detect actual changes vs unchanged re-scrapes

### Enrichment

- [x] **ENRC-01**: LLM enrichment classifies industry (tech, fintech, healthtech, etc.)
- [x] **ENRC-02**: LLM enrichment estimates company size (startup, midsize, large)
- [x] **ENRC-03**: LLM enrichment extracts likely required skills
- [x] **ENRC-04**: LLM enrichment estimates visa sponsorship likelihood
- [x] **ENRC-05**: Content-hash gating: only enrich new or changed jobs (skip unchanged)
- [ ] **ENRC-06**: Embedding generation via OpenAI text-embedding-3-small for each job
- [ ] **ENRC-07**: Embedding stored in pgvector column for ANN retrieval
- [ ] **ENRC-08**: Enrichment stores embedding_model identifier for drift tracking
- [x] **ENRC-09**: Rate limiting and retry logic for Anthropic and OpenAI API calls
- [x] **ENRC-10**: Structured output validation (null/unknown as first-class values, not hallucinated guesses)

### Matching

- [ ] **MTCH-01**: Hard filter by job_type (intern / new_grad)
- [ ] **MTCH-02**: Hard filter by sponsorship requirement
- [ ] **MTCH-03**: Fuzzy location matching with normalization (SF/San Francisco, NYC/New York, Remote)
- [ ] **MTCH-04**: Title similarity scoring via embedding cosine similarity (weight: 0.30)
- [ ] **MTCH-05**: Skills overlap scoring — user skills vs job required skills (weight: 0.25)
- [ ] **MTCH-06**: Industry match scoring (weight: 0.15)
- [ ] **MTCH-07**: Company size preference scoring (weight: 0.10)
- [ ] **MTCH-08**: Location fit scoring (weight: 0.10)
- [ ] **MTCH-09**: Recency scoring — newer posts rank higher (weight: 0.05)
- [ ] **MTCH-10**: Feedback boost scoring from past likes/dislikes (weight: 0.05)
- [ ] **MTCH-11**: Returns top-N ranked jobs with individual signal breakdown per match
- [ ] **MTCH-12**: Library API entry point: `get_matches(profile, top_n=30) -> list[dict]`
- [ ] **MTCH-13**: Cold-start mode for users with no feedback history (neutral feedback signal)

### Feedback

- [ ] **FDBK-01**: Record like/dislike/applied reactions per user per job
- [ ] **FDBK-02**: Like updates liked_companies list on user profile
- [ ] **FDBK-03**: Dislike updates disliked_companies list on user profile
- [ ] **FDBK-04**: Affinity embedding updated as weighted running average of liked job embeddings
- [ ] **FDBK-05**: Feedback signal incorporated into matching score computation

### Integration

- [ ] **INTG-01**: End-to-end test script: scrape → enrich → match against test profile
- [ ] **INTG-02**: Cron-ready scraper script (daily 6 AM ET)
- [ ] **INTG-03**: Cron-ready enrichment script (daily 6:30 AM ET)
- [ ] **INTG-04**: All components importable as Python library (no HTTP server required)
- [ ] **INTG-05**: .env.example with all required environment variables documented

## v2 Requirements

### API Server

- **API-01**: FastAPI HTTP wrapper around matching engine
- **API-02**: REST endpoints for match, feedback, profile CRUD

### Advanced Matching

- **ADV-01**: Feedback decay — older feedback has less weight
- **ADV-02**: Diversity injection — prevent filter bubble in results
- **ADV-03**: Collaborative filtering from aggregate user behavior

### Data Sources

- **DATA-01**: Additional job sources beyond SimplifyJobs
- **DATA-02**: Company metadata enrichment from external APIs (Crunchbase, LinkedIn)

## Out of Scope

| Feature | Reason |
|---------|--------|
| Discord bot integration | Separate spec exists, deferred to own project |
| Web dashboard | Frontend-agnostic engine — frontends consume the library |
| Resume parsing | High complexity, tangential to core matching |
| Email notifications | Delivery layer, not matching layer |
| User authentication | Caller provides profile directly — auth belongs in frontend |
| Real-time streaming scrape | Source data updates daily, streaming adds no value |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| FOUND-01 | Phase 1 — Foundation | Complete |
| FOUND-02 | Phase 1 — Foundation | Complete |
| FOUND-03 | Phase 1 — Foundation | Complete |
| FOUND-04 | Phase 1 — Foundation | Complete |
| FOUND-05 | Phase 1 — Foundation | Complete |
| FOUND-06 | Phase 1 — Foundation | Complete |
| FOUND-07 | Phase 1 — Foundation | Complete |
| FOUND-08 | Phase 1 — Foundation | Complete |
| SCRP-01 | Phase 2 — Scraper | Complete |
| SCRP-02 | Phase 2 — Scraper | Complete |
| SCRP-03 | Phase 2 — Scraper | Complete |
| SCRP-04 | Phase 2 — Scraper | Complete |
| SCRP-05 | Phase 2 — Scraper | Complete |
| SCRP-06 | Phase 2 — Scraper | Complete |
| SCRP-07 | Phase 2 — Scraper | Complete |
| SCRP-08 | Phase 2 — Scraper | Complete |
| SCRP-09 | Phase 2 — Scraper | Complete |
| ENRC-01 | Phase 3 — LLM Enrichment | Complete |
| ENRC-02 | Phase 3 — LLM Enrichment | Complete |
| ENRC-03 | Phase 3 — LLM Enrichment | Complete |
| ENRC-04 | Phase 3 — LLM Enrichment | Complete |
| ENRC-05 | Phase 3 — LLM Enrichment | Complete |
| ENRC-06 | Phase 4 — Embeddings | Pending |
| ENRC-07 | Phase 4 — Embeddings | Pending |
| ENRC-08 | Phase 4 — Embeddings | Pending |
| ENRC-09 | Phase 3 — LLM Enrichment | Complete |
| ENRC-10 | Phase 3 — LLM Enrichment | Complete |
| MTCH-01 | Phase 5 — Hard Filters | Pending |
| MTCH-02 | Phase 5 — Hard Filters | Pending |
| MTCH-03 | Phase 5 — Hard Filters | Pending |
| MTCH-04 | Phase 6 — Scoring Engine | Pending |
| MTCH-05 | Phase 6 — Scoring Engine | Pending |
| MTCH-06 | Phase 6 — Scoring Engine | Pending |
| MTCH-07 | Phase 6 — Scoring Engine | Pending |
| MTCH-08 | Phase 6 — Scoring Engine | Pending |
| MTCH-09 | Phase 6 — Scoring Engine | Pending |
| MTCH-10 | Phase 6 — Scoring Engine | Pending |
| MTCH-11 | Phase 6 — Scoring Engine | Pending |
| MTCH-12 | Phase 6 — Scoring Engine | Pending |
| MTCH-13 | Phase 6 — Scoring Engine | Pending |
| FDBK-01 | Phase 7 — Feedback Loop | Pending |
| FDBK-02 | Phase 7 — Feedback Loop | Pending |
| FDBK-03 | Phase 7 — Feedback Loop | Pending |
| FDBK-04 | Phase 7 — Feedback Loop | Pending |
| FDBK-05 | Phase 7 — Feedback Loop | Pending |
| INTG-01 | Phase 8 — Integration & Operations | Pending |
| INTG-02 | Phase 8 — Integration & Operations | Pending |
| INTG-03 | Phase 8 — Integration & Operations | Pending |
| INTG-04 | Phase 8 — Integration & Operations | Pending |
| INTG-05 | Phase 8 — Integration & Operations | Pending |

**Coverage:**
- v1 requirements: 46 total
- Mapped to phases: 46
- Unmapped: 0

---
*Requirements defined: 2026-03-25*
*Last updated: 2026-03-25 after roadmap creation*
