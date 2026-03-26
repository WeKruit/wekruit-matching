# Architecture Research

**Domain:** Job scraping + matching engine (Python backend, no frontend)
**Researched:** 2026-03-25
**Confidence:** HIGH

---

## Standard Architecture

### System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INGESTION LAYER                             │
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐   │
│  │  GitHub Raw  │    │   Scraper    │    │   Enrichment Worker  │   │
│  │   README     │───►│   (httpx +   │───►│  (Anthropic classify │   │
│  │  (markdown)  │    │   parser)    │    │   + OAI embeddings)  │   │
│  └──────────────┘    └──────┬───────┘    └──────────┬───────────┘   │
│                             │                       │               │
│                     [raw Job records]      [enriched metadata       │
│                      + stable IDs          + 1536-dim vectors]      │
└─────────────────────────────┼───────────────────────┼───────────────┘
                              │                       │
                              ▼                       ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         STORAGE LAYER                               │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    PostgreSQL + pgvector                      │   │
│  │                                                               │   │
│  │   jobs table                    user_profiles table           │   │
│  │   ├── id (stable hash)          ├── user_id                  │   │
│  │   ├── title, company, url       ├── skills[], preferences    │   │
│  │   ├── location, sponsorship     ├── feedback_history[]       │   │
│  │   ├── status (active/stale)     └── affinity_embedding       │   │
│  │   ├── metadata JSONB                                          │   │
│  │   └── embedding vector(1536)    feedback table               │   │
│  │                                 ├── user_id, job_id          │   │
│  │                                 └── signal (like/dislike)    │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         MATCHING LAYER                              │
│                                                                     │
│  ┌─────────────────┐    ┌──────────────────┐    ┌───────────────┐   │
│  │  User Profile   │    │  Matching Engine  │    │  Ranked Job   │   │
│  │  JSON (input)   │───►│  1. Hard filters  │───►│  List (output)│   │
│  │                 │    │  2. ANN retrieve  │    │  (top-N with  │   │
│  └─────────────────┘    │  3. Multi-signal  │    │   scores)     │   │
│                          │     score         │    └───────────────┘   │
│                          └──────────────────┘                        │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Boundary |
|-----------|----------------|----------|
| **Scraper** | Fetches raw GitHub README markdown, parses job table rows, generates stable IDs, skips closed listings (lock emoji) | Outputs normalized Job records; knows nothing about enrichment or matching |
| **Enrichment Worker** | Calls Anthropic to classify industry/company size/skills/sponsorship; calls OpenAI to generate embeddings; writes enriched records to DB | Reads unenriched jobs, writes enriched jobs; never touches user profiles |
| **Job DB** | Persists jobs with enriched metadata and vector embeddings; handles upsert (insert new, update changed, mark stale inactive) | Single source of truth for jobs; never invokes business logic |
| **Matching Engine** | Accepts user profile, applies hard filters, retrieves ANN candidates via pgvector, scores with multi-signal weighted formula, returns ranked list | Reads from DB; never scrapes; never calls enrichment APIs |
| **User Profile** | Input-only record: skills, preferences, experience, feedback history, affinity embedding | Provided by caller; updated only via feedback endpoint |
| **Feedback Handler** | Records like/dislike signals, adjusts user preference weights and affinity embedding | Writes to DB; triggers no scraping or enrichment |

---

## Recommended Project Structure

```
wekruit-matching/
├── scraper/
│   ├── fetch.py            # httpx calls to GitHub raw README URLs
│   ├── parse.py            # markdown table parser, row extractor
│   ├── id.py               # stable ID generation (hash of company+title+url)
│   └── run.py              # cron entrypoint: fetch → parse → upsert
│
├── enrichment/
│   ├── classify.py         # Anthropic prompt: extract industry, skills, etc.
│   ├── embed.py            # OpenAI text-embedding-3-small calls
│   ├── cache.py            # skip already-enriched jobs (check DB state)
│   └── run.py              # cron entrypoint: find unenriched → enrich → write
│
├── db/
│   ├── schema.sql          # table definitions, pgvector extension, indexes
│   ├── client.py           # connection pool, query helpers
│   └── upsert.py           # upsert logic: insert/update/mark stale
│
├── matching/
│   ├── filters.py          # hard filter: job type, sponsorship, location blocklist
│   ├── retrieve.py         # pgvector ANN query: get top-K candidates
│   ├── score.py            # weighted multi-signal scorer
│   ├── location.py         # fuzzy location normalization (SF/San Francisco, etc.)
│   └── api.py              # match(user_profile) → ranked list; library entrypoint
│
├── feedback/
│   └── handler.py          # record signal, update user embedding + weights
│
├── models/
│   ├── job.py              # Job dataclass
│   ├── user_profile.py     # UserProfile dataclass
│   └── match_result.py     # MatchResult dataclass (job + score breakdown)
│
├── config.py               # env vars: DB URL, API keys, weights
└── tests/
    └── e2e_test.py         # full pipeline smoke test
```

### Structure Rationale

- **scraper/ vs enrichment/ separation:** Scraping is network I/O against GitHub (rate-limit profile: moderate, predictable). Enrichment is LLM API calls (rate-limit profile: expensive, slow, must cache). Combining them would force re-enrichment on scrape failures or conflate two very different failure modes.
- **db/upsert.py standalone:** Upsert logic is the most complex DB operation (detect changed rows, mark stale). Isolating it means both the scraper and enrichment worker import it independently without coupling.
- **matching/ is a library, not a service:** No HTTP server. Callers (Discord bot, web app) import `matching/api.py` directly. This keeps this repo backend-only per the project constraint.
- **models/ shared dataclasses:** All three modules (scraper, matching, feedback) use the same Job and UserProfile types. Putting them in models/ avoids circular imports.

---

## Architectural Patterns

### Pattern 1: Stable ID for Deduplication

**What:** Hash-based deterministic job ID derived from company + title + application URL (not row position in README).
**When to use:** Any time a job listing can appear in multiple scrape runs, or across multiple source repos (Summer2026-Internships and New-Grad-Positions may overlap).
**Trade-offs:** Simple and stateless. Fails if company renames role slightly — acceptable for this domain.

**Example:**
```python
import hashlib

def make_job_id(company: str, title: str, url: str) -> str:
    key = f"{company.lower().strip()}|{title.lower().strip()}|{url.strip()}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
```

### Pattern 2: Enrich-Only-New Jobs (Conditional Enrichment)

**What:** Before calling LLM APIs, check if a job already has enriched metadata and an embedding. Skip if present and unchanged.
**When to use:** Every enrichment run. LLM calls are expensive; scraping happens daily on a corpus of 1000+ listings.
**Trade-offs:** Saves ~90% of LLM cost. Adds a DB read before each potential LLM call — negligible overhead.

**Example:**
```python
def needs_enrichment(job_id: str, db) -> bool:
    row = db.fetch_one("SELECT enriched_at FROM jobs WHERE id = %s", job_id)
    return row is None or row["enriched_at"] is None
```

### Pattern 3: Two-Stage Match (Filter → Score)

**What:** Apply hard boolean filters first (job type, sponsorship, excluded locations), then run ANN vector retrieval on the surviving set, then apply weighted multi-signal scoring.
**When to use:** Always. Hard filters eliminate structurally incompatible jobs before any expensive computation. ANN retrieval narrows to semantically relevant candidates. Weighted scoring ranks the shortlist by all signals.
**Trade-offs:** Three passes over data — but each pass is dramatically smaller than the prior one. Hard filters are O(N) SQL with indexed columns. ANN is O(log N) via HNSW. Weighted scoring is O(K) where K is the candidate count (typically 50-200).

```
All jobs (N=~2000)
   ↓ hard filters (SQL WHERE clauses)
Eligible jobs (maybe 400)
   ↓ pgvector ANN (<-> cosine distance, LIMIT 100)
Candidates (100)
   ↓ weighted score: title_sim×0.30 + skills×0.25 + industry×0.15 + ...
Ranked list (top 20 returned)
```

### Pattern 4: Feedback Embedding Shift

**What:** When a user likes a job, nudge their affinity embedding toward that job's embedding. When they dislike, nudge away. Lerp by a small factor (e.g., 0.05).
**When to use:** After any like/dislike signal.
**Trade-offs:** Simple online update without retraining. Embedding drift over many likes is possible — mitigated by also keeping explicit preference fields.

---

## Data Flow

### Ingestion Flow (Daily Cron)

```
[GitHub Raw README URL]
    ↓ httpx GET (no auth required for public repos)
[Raw Markdown Text]
    ↓ parse.py — extract table rows, skip lock-emoji rows
[Job records: title, company, url, location, sponsorship_flag, date]
    ↓ id.py — generate stable hash ID
    ↓ db/upsert.py — INSERT ON CONFLICT UPDATE, mark missing rows stale
[Jobs table: raw fields written, enriched_at = NULL for new rows]
    ↓ enrichment/run.py — query WHERE enriched_at IS NULL
[Unenriched jobs]
    ↓ classify.py — Anthropic batch: industry, company_size, skills[], remote_ok
    ↓ embed.py — OpenAI text-embedding-3-small on (title + company + skills)
[Enriched metadata + embedding vector]
    ↓ db/upsert.py — UPDATE jobs SET metadata=..., embedding=..., enriched_at=NOW()
[Jobs table: fully enriched, queryable]
```

### Match Request Flow (On-Demand)

```
[Caller: UserProfile JSON]
    ↓ matching/api.py — validate profile, generate query embedding if absent
[Query embedding (1536-dim)]
    ↓ matching/filters.py — build SQL WHERE for job_type, sponsorship, location
[Hard filter clause]
    ↓ matching/retrieve.py — pgvector ANN: WHERE <filters> ORDER BY embedding <-> query_vec LIMIT 100
[Candidate job list (≤100)]
    ↓ matching/score.py — compute 7-signal weighted score per candidate
[Scored candidates]
    ↓ sort descending, slice top-N
[MatchResult list: job + score + per-signal breakdown]
    ↓ return to caller
```

### Feedback Flow

```
[Caller: user_id, job_id, signal (like/dislike)]
    ↓ feedback/handler.py
[Record in feedback table]
    ↓ fetch user affinity_embedding and job embedding
    ↓ nudge: new_affinity = lerp(affinity, job_embedding, α=0.05) if like
              new_affinity = lerp(affinity, -job_embedding, α=0.05) if dislike
    ↓ UPDATE user_profiles SET affinity_embedding=..., feedback_history=...
[User profile updated — next match call uses shifted embedding]
```

---

## Build Order

Components have hard dependencies. Build in this order:

```
Phase 1: DB schema + client
    └── Everything else depends on this.

Phase 2: Scraper (fetch + parse + id + upsert)
    └── Produces raw job data. Enrichment depends on this.

Phase 3: Enrichment (classify + embed + conditional cache)
    └── Produces enriched jobs + embeddings. Matching depends on embeddings.

Phase 4: Matching engine (filters + retrieve + score + location normalization)
    └── Depends on enriched jobs in DB. User profiles can be hardcoded for testing.

Phase 5: Feedback handler
    └── Depends on matching (need to know what jobs exist) and user profiles.

Phase 6: End-to-end test + cron wiring
    └── Validates full pipeline. Depends on all prior phases.
```

Parallel where possible:
- Scraper and DB schema can be developed together (schema first, scraper writes to it).
- Matching engine logic (score.py, location.py) can be unit-tested against mock data before the DB is populated.

---

## Integration Points

### External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| GitHub Raw README | `httpx.get(raw_url)` — no auth, no JS, direct markdown | URL format: `https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md` — pin to `dev` branch |
| Anthropic API | Single-turn classification prompt per job batch | Use `claude-haiku` for cost; structure output as JSON via tool use or response format |
| OpenAI Embeddings | `text-embedding-3-small`, batch up to 2048 tokens | Batch multiple job texts per API call; cache by job ID to avoid re-embedding |
| PostgreSQL + pgvector | psycopg3 with pgvector extension | `CREATE EXTENSION IF NOT EXISTS vector;` must run at schema init |

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| Scraper → DB | Direct function call via `db/upsert.py` | Scraper never calls enrichment directly |
| Enrichment Worker → DB | Direct function call via `db/client.py` + `db/upsert.py` | Worker queries DB for unenriched rows; no IPC with scraper |
| Matching Engine → DB | Read-only SQL queries via `db/client.py` | Matching never writes to jobs table |
| Feedback Handler → DB | Read/write via `db/client.py` | Only writes to user_profiles and feedback tables |
| Caller → Matching | Python function call: `from matching.api import match` | No HTTP server; no serialization overhead |

---

## Scaling Considerations

| Scale | Architecture Adjustments |
|-------|--------------------------|
| 0-5K jobs, few users | Single Postgres instance, synchronous enrichment in cron job, in-process matching. Current design handles this well. |
| 5K-50K jobs, hundreds of users | Add HNSW index on embedding column (`CREATE INDEX ON jobs USING hnsw (embedding vector_cosine_ops)`). Move enrichment to async task queue (e.g., asyncio batch). |
| 50K+ jobs, thousands of users | Consider read replica for matching queries. Pre-compute candidate sets per user at cron time ("push model") rather than computing on request. |

### Scaling Priorities

1. **First bottleneck: LLM enrichment cost.** At 2000 jobs/day, even haiku calls add up. Conditional enrichment (skip already-enriched) is the primary mitigation. Batch API calls reduce per-call overhead.
2. **Second bottleneck: ANN query latency at high job count.** Add HNSW index before the job table exceeds ~10K rows. pgvector HNSW handles 1M+ vectors efficiently with the right configuration.

---

## Anti-Patterns

### Anti-Pattern 1: Enriching on Every Scrape Run

**What people do:** Call LLM classify + embed for all jobs on every daily scrape, not just new ones.
**Why it's wrong:** 1000-job corpus at $0.001/job = $1/day = $365/year for zero new information on unchanged jobs.
**Do this instead:** `WHERE enriched_at IS NULL` before calling any LLM API. Only enrich jobs that are new or have changed significantly (title/company/url changed = new hash = new row).

### Anti-Pattern 2: Storing Embeddings Outside the Job DB

**What people do:** Use a separate vector store (Pinecone, Weaviate, Chroma) alongside Postgres for the structured data.
**Why it's wrong:** Two databases to keep in sync. Structured filters (sponsorship, job type) require a round-trip: SQL → vector store → SQL. Adds ops overhead for no benefit at this scale.
**Do this instead:** pgvector in the same Postgres instance. Single query: `WHERE sponsorship = true ORDER BY embedding <-> $1 LIMIT 100` combines filter and ANN in one SQL statement.

### Anti-Pattern 3: Pure Embedding Similarity as the Only Signal

**What people do:** Match = cosine similarity between user embedding and job embedding, nothing else.
**Why it's wrong:** Embeddings capture semantic meaning but miss hard constraints. A user who needs visa sponsorship will get matches at sponsoring companies ranked #50 behind unsponored roles that are semantically closer.
**Do this instead:** Hard filter sponsorship/job-type first, then use embedding similarity as one of seven weighted signals. Explicit preference signals (location, company size) get their own terms in the scoring formula.

### Anti-Pattern 4: Parsing README by Row Index

**What people do:** Extract row N from the markdown table, assume it maps to job N in DB.
**Why it's wrong:** The SimplifyJobs README is edited by many contributors. Rows shift, get reordered, get deleted. Row-index-based IDs break on every table edit.
**Do this instead:** Stable ID = hash of (company + title + application URL). URL is stable and unique per job application link.

---

## Sources

- [GitHub - rabiuk/job-scraper: SimplifyJobs scraper architecture](https://github.com/rabiuk/job-scraper)
- [pgvector: Open-source vector similarity search for Postgres](https://github.com/pgvector/pgvector)
- [Hybrid Search in PostgreSQL (pgvector + SQL filters)](https://www.paradedb.com/blog/hybrid-search-in-postgresql-the-missing-manual)
- [The 3-Stage Funnel Behind Every Modern Recommender System](https://www.mlwhiz.com/p/the-recommendation-engine-under-the)
- [5 Patterns for Scalable LLM Service Integration](https://latitude.so/blog/5-patterns-for-scalable-llm-service-integration)
- [Aman's AI Journal: Recommendation Systems Ranking/Scoring](https://aman.ai/recsys/ranking/)
- [Technical Job Recommendation System Using APIs and Web Crawling (PMC)](https://pmc.ncbi.nlm.nih.gov/articles/PMC9239795/)
- [Data Pipeline Architecture Patterns — Alation](https://www.alation.com/blog/data-pipeline-architecture-patterns/)
- [Web Scraping for Data Engineers — Production Pipeline Architecture (Medium, 2026)](https://htrixe.medium.com/web-scraping-for-data-engineers-architecture-robustness-and-production-pipelines-with-scrapling-c327278222f7)

---
*Architecture research for: WeKruit Matching Engine (job scraping + matching)*
*Researched: 2026-03-25*
