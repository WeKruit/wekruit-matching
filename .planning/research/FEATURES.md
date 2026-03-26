# Feature Research

**Domain:** Job scraping and matching engine (backend API, no frontend)
**Researched:** 2026-03-25
**Confidence:** HIGH (core pipeline), MEDIUM (differentiators), MEDIUM (anti-features)

---

## Feature Landscape

### Table Stakes (Users Expect These)

These are the non-negotiable features for a job scraping and matching backend. Any API consumer (Discord bot, web app) that plugs in expects these to work correctly. Missing any one of them makes the engine unreliable or useless.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| SimplifyJobs GitHub scraping | The entire data source — without this there is no engine | MEDIUM | Parse `listings.json` (JSON, not raw README markdown), pull from Summer2026-Internships and New-Grad-Positions repos via raw GitHub URLs; httpx suffices, no browser needed |
| Closed listings filter | Inactive jobs waste match slots and frustrate users | LOW | SimplifyJobs uses an `active` boolean field in `listings.json`; filter server-side, never surface closed rows |
| Stable job ID generation | Deduplication requires consistent identity across scrape runs | LOW | Hash on `company_name + title + url`; SimplifyJobs also provides an `id` field that can be used as stable key |
| Upsert with staleness marking | Jobs appear, change, and expire — the DB must reflect reality | MEDIUM | INSERT on new, UPDATE on changed fields, set `is_active = false` for IDs absent from latest scrape; use Postgres `ON CONFLICT DO UPDATE` |
| LLM-based metadata enrichment | Structured metadata (industry, company size, sponsorship, skills) is not in the raw listing | HIGH | Batch enrichment via Anthropic Claude; only call LLM on new or changed jobs — never re-enrich unchanged rows (cost) |
| Embedding generation per job | Semantic matching requires vector representation | LOW | `text-embedding-3-small` via OpenAI; store in pgvector column alongside structured data |
| User profile schema | The engine needs a stable representation of who to match against | MEDIUM | Skills list, preferences (job type, location, company size, sponsorship required), experience level, career goals, feedback history |
| Weighted multi-signal scoring | Matching must produce a ranked list ordered by fit, not random | HIGH | Blend: title_similarity (0.30), skills_overlap (0.25), industry_match (0.15), company_size_match (0.10), location_fit (0.10), recency (0.05), feedback_boost (0.05); each signal normalized 0–1 before weighting |
| Hard filter enforcement | Some mismatches are dealbreakers, not soft penalties | LOW | Job type filter, sponsorship requirement, excluded locations; apply BEFORE scoring, not as a negative weight |
| Location normalization | "SF", "San Francisco", "San Francisco, CA" must resolve to the same bucket | LOW | Alias map + normalization function; covers top-20 US tech hubs and Remote variants |
| Ranked results API | Callers need an ordered list with scores, not a raw dump | LOW | Return list of `{job_id, score, breakdown}` sorted descending by composite score; include signal-level breakdown for debugging |
| Cron-ready scrape entrypoint | Engine must refresh data on a schedule without manual intervention | LOW | Scraper and enrichment scripts must be callable as standalone CLI entrypoints with no side effects outside DB |

### Differentiators (Competitive Advantage)

These features are not found in generic job scrapers. They are what makes the engine useful specifically for WeKruit's use case — personalized matching for students and new grads against a curated, enriched dataset.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Feedback-driven preference update | Like/dislike signals improve future matches without re-onboarding the user | MEDIUM | On `like`: upweight matched signals and update user affinity embedding. On `dislike`: downweight. Store feedback as event log, compute aggregate in `feedback_boost` signal |
| User affinity embedding | Captures implicit taste that explicit preferences miss | MEDIUM | Maintain per-user "ideal job" embedding as a rolling weighted average of liked job embeddings; cosine similarity with candidate jobs contributes to semantic signal |
| Signal-level score breakdown | Callers can explain WHY a job was ranked high — critical for trust | LOW | Return per-signal scores alongside composite: `{title: 0.8, skills: 0.6, location: 1.0, ...}`; no added compute cost since signals are already computed |
| Incremental enrichment (cost-aware) | Only enrich new/changed jobs — avoid burning LLM budget on re-processing | MEDIUM | Hash job content at scrape time, compare against stored hash in DB, skip enrichment if unchanged; target: <5% of jobs per day require re-enrichment |
| Sponsorship classification | Students on visas need this as a hard filter — no other open-source scraper enriches it | MEDIUM | LLM prompt to classify `sponsorship_offered: true/false/unknown` from job text; "We are unable to sponsor" patterns are reliable negative signals |
| Fuzzy skill matching | "React.js", "ReactJS", "React" must match as the same skill | LOW | Normalize skill tokens before overlap comparison: lowercase, strip punctuation, alias map for known variants (JS/JavaScript, ML/Machine Learning) |
| Recency decay scoring | Fresh postings should rank above stale ones controlling for quality | LOW | Exponential decay: score = base_score * e^(-lambda * days_since_posted); lambda tunable, default 0.05 gives ~50% decay at 14 days |

### Anti-Features (Commonly Requested, Often Problematic)

These features get requested because they seem natural extensions, but each adds disproportionate complexity relative to value for this backend-only engine.

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Resume parsing | "Match against my resume, not a profile" | Adds PDF/DOCX parsing, OCR edge cases, unstructured extraction complexity; resume format varies wildly; out of scope per PROJECT.md | Caller builds resume-to-profile conversion if needed; engine accepts normalized profile struct |
| Real-time streaming scrape | "Give me jobs the second they're posted" | SimplifyJobs updates via GitHub Actions batch (~daily); polling faster than source update frequency adds load with zero benefit; GitHub rate limits unauthenticated requests | Cron daily scrape aligned to SimplifyJobs update cadence; expose `scraped_at` timestamp so callers know freshness |
| User authentication and sessions | "The engine should manage user accounts" | Auth is a cross-cutting concern; every client (Discord bot, web app) has different auth needs; adding it here forces all consumers to use the same model | Engine is a library — caller passes `user_id` and profile directly; auth lives in the consumer layer per PROJECT.md |
| Web dashboard / admin UI | "Show me all jobs in a table" | Frontend is explicitly out of scope; building it here couples concerns and doubles the project scope | Expose read endpoints; consumer builds the view |
| Multi-source aggregation (LinkedIn, Indeed) | "Scrape more sources for more jobs" | Anti-bot measures, terms of service violations, browser automation overhead; SimplifyJobs is already curated and structured | Start with SimplifyJobs; design data model to be source-agnostic so additional sources can be added later without schema changes |
| Salary filtering and normalization | "Filter by salary range" | SimplifyJobs listings rarely include salary; LLM inference on absent data is unreliable; adds a filter that returns empty results silently | Omit until a reliable salary signal exists in the source data |
| Collaborative filtering ("users like you also applied to") | "Improve matching with social signals" | Requires user-level interaction graph; privacy concerns; cold start problem for new users; significant ML infrastructure | Feedback loop via per-user affinity embedding achieves personalization without requiring other users' data |
| Keyword search endpoint | "Let me search by keyword" | Duplicate of what pgvector semantic search already does better; keyword search returns false positives (job contains the word but is irrelevant) | Use semantic embedding similarity search; expose it as the primary search mechanism |

---

## Feature Dependencies

```
[SimplifyJobs Scraper]
    └──produces──> [Job Records in Postgres]
                       ├──requires──> [Stable ID Generation]
                       ├──requires──> [Upsert + Staleness Marking]
                       ├──feeds──> [LLM Enrichment]
                       │               └──produces──> [Structured Metadata]
                       │               └──produces──> [Sponsorship Classification]
                       └──feeds──> [Embedding Generation]
                                       └──stores in──> [pgvector column]

[User Profile Schema]
    └──feeds──> [Weighted Multi-Signal Scoring]
                    ├──requires──> [Job Records with Metadata]
                    ├──requires──> [Hard Filter Enforcement]
                    ├──requires──> [Location Normalization]
                    ├──requires──> [Fuzzy Skill Matching]
                    └──produces──> [Ranked Results API]

[Feedback Loop]
    ├──requires──> [User Profile Schema]
    ├──requires──> [Ranked Results API] (user must see results to like/dislike)
    ├──updates──> [User Affinity Embedding]
    └──feeds into──> [feedback_boost signal in scoring]

[User Affinity Embedding]
    ├──requires──> [Embedding Generation] (job embeddings must exist)
    └──enhances──> [Weighted Multi-Signal Scoring]

[Incremental Enrichment]
    ├──requires──> [SimplifyJobs Scraper]
    └──gates──> [LLM Enrichment] (only runs when content hash changes)

[Cron-Ready Entrypoint]
    ├──requires──> [SimplifyJobs Scraper]
    ├──requires──> [LLM Enrichment]
    └──requires──> [Embedding Generation]
```

### Dependency Notes

- **Embedding Generation requires Job Records:** Embeddings are generated from enriched job text; run enrichment before embedding generation in the pipeline.
- **Weighted Scoring requires Hard Filters first:** Apply hard filters (sponsorship, job type, location exclusions) before computing weighted scores to avoid scoring jobs that will be discarded anyway.
- **Feedback Loop requires Ranked Results:** Users cannot express feedback without first seeing match results; feedback is a v1 feature but depends on the core scoring pipeline being stable first.
- **Incremental Enrichment gates LLM Enrichment:** The content hash check is a guard — skip LLM call if hash unchanged. This must be implemented early or LLM costs will compound with each scrape run.
- **Fuzzy Skill Matching requires normalization before scoring:** Skill normalization must run at both ingest time (for stored job skills) and query time (for user profile skills) to produce consistent overlap scores.

---

## MVP Definition

### Launch With (v1)

Minimum viable — what's needed to produce a working ranked job list for a user profile.

- [ ] SimplifyJobs scraper (listings.json, both repos) — the data source, everything depends on this
- [ ] Stable ID generation and upsert with staleness marking — without this the DB drifts from reality
- [ ] LLM enrichment with incremental hash check — metadata needed for scoring; hash check needed for cost control
- [ ] Embedding generation and pgvector storage — needed for semantic signal in scoring
- [ ] Location normalization — without this location_fit signal is unreliable
- [ ] Fuzzy skill normalization — skills overlap is the highest-weighted signal (0.25)
- [ ] Hard filter enforcement (sponsorship, job type, location) — filters must precede scoring
- [ ] Weighted multi-signal scoring engine — core value proposition
- [ ] Ranked results API with signal breakdown — what callers consume
- [ ] Cron-ready entrypoint — makes the system operational, not just a script

### Add After Validation (v1.x)

Add these once the core matching pipeline is confirmed working and producing quality results.

- [ ] Feedback loop (like/dislike) — trigger: at least one consumer (Discord bot) is sending real user interactions
- [ ] User affinity embedding — trigger: feedback data exists; affinity embedding is only meaningful once likes/dislikes accumulate
- [ ] Recency decay tuning — trigger: recency signal is live but initial lambda value may need adjustment based on observed result quality

### Future Consideration (v2+)

Defer until there is evidence of user demand and product-market fit.

- [ ] Additional data sources (other than SimplifyJobs) — defer until SimplifyJobs coverage is insufficient for users
- [ ] Skill gap recommendations ("you're missing X skill for this job") — complex, requires O*NET or similar taxonomy
- [ ] Batch profile matching (match N profiles against M jobs) — only needed at scale; current design handles one profile at a time
- [ ] A/B weight testing framework — only needed if tuning weights becomes a recurring need

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| SimplifyJobs scraper | HIGH | MEDIUM | P1 |
| Stable ID + upsert + staleness marking | HIGH | LOW | P1 |
| LLM enrichment (with hash check) | HIGH | HIGH | P1 |
| Embedding generation (pgvector) | HIGH | LOW | P1 |
| Location normalization | HIGH | LOW | P1 |
| Fuzzy skill normalization | HIGH | LOW | P1 |
| Hard filter enforcement | HIGH | LOW | P1 |
| Weighted multi-signal scoring | HIGH | HIGH | P1 |
| Ranked results API with breakdown | HIGH | LOW | P1 |
| Cron-ready entrypoint | HIGH | LOW | P1 |
| Sponsorship classification (LLM) | HIGH | MEDIUM | P1 |
| Feedback loop (like/dislike) | MEDIUM | MEDIUM | P2 |
| User affinity embedding | MEDIUM | MEDIUM | P2 |
| Recency decay scoring | MEDIUM | LOW | P2 |
| Signal-level score breakdown | MEDIUM | LOW | P2 |
| Incremental enrichment (hash gate) | MEDIUM | LOW | P2 |
| Multi-source aggregation | LOW | HIGH | P3 |
| Resume parsing | LOW | HIGH | P3 |
| Salary normalization and filter | LOW | HIGH | P3 |
| Collaborative filtering | LOW | HIGH | P3 |

**Priority key:**
- P1: Must have for launch
- P2: Should have, add when possible
- P3: Nice to have, future consideration

---

## Competitor Feature Analysis

The relevant "competitors" for this engine are open-source job scraper projects and commercial job board matching APIs. The WeKruit engine is a private, opinionated backend — not a general-purpose platform — so differentiation is measured against what these tools do and where they fall short.

| Feature | rabiuk/job-scraper (open-source) | Google Cloud Talent Solution (commercial) | WeKruit Matching Engine |
|---------|----------------------------------|-------------------------------------------|------------------------|
| SimplifyJobs source | Yes (GitHub + Simplify.jobs) | No | Yes |
| Deduplication | JSON state file (in-memory across runs) | Built-in | Postgres upsert by stable ID |
| LLM enrichment | No | Partial (NLP title normalization) | Yes (Claude, full classification) |
| Embeddings / semantic matching | No | Yes (proprietary) | Yes (text-embedding-3-small + pgvector) |
| Weighted multi-signal scoring | No (notification only) | Yes (opaque model) | Yes (transparent, tunable weights) |
| Hard filters | No | Yes | Yes |
| Feedback loop | No | Yes (implicit signals) | Yes (explicit like/dislike) |
| Sponsorship classification | No | No | Yes (LLM-classified) |
| Cost model | Free (DIY) | Pay per query | Minimize via hash-gated enrichment |
| Transparency | High (open source) | Low (black box) | High (signal breakdown in response) |

**Key gap filled by WeKruit:** No open-source tool combines SimplifyJobs scraping + LLM enrichment + pgvector semantic matching + weighted scoring + feedback loops in a single coherent backend. The engine addresses exactly this gap.

---

## Sources

- [SimplifyJobs Summer2026-Internships repository structure](https://github.com/SimplifyJobs/Summer2026-Internships) — confirmed `listings.json` format with `active`, `id`, `company_name`, `locations`, `title`, `date_posted`, `url` fields
- [rabiuk/job-scraper — open-source SimplifyJobs scraper](https://github.com/rabiuk/job-scraper) — deduplication via seen_jobs.json, polling pattern, Discord webhook
- [Eightfold AI engineering blog — talent matching architecture](https://eightfold.ai/engineering-blog/ai-powered-talent-matching-the-tech-behind-smarter-and-fairer-hiring/) — multi-signal scoring: skill overlap, career trajectory, company similarity, recency
- [Real-Time Adaptive Job Recommendations via RL (IJERT)](https://www.ijert.org/real-time-adaptive-job-recommendations-using-reinforcement-learning-based-on-user-interaction-feedback-ijertconv14is010054) — feedback loop signals: view, click, dismiss, apply → reward signal
- [Job Matching Algorithms overview (mokahr.io)](https://www.mokahr.io/myblog/job-matching-algorithms/) — weighted scoring formula, ensemble approaches
- [Semantic Job Matching with pgvector (PostgreSQL Fastware)](https://www.postgresql.fastware.com/blog/from-embeddings-to-answers) — embedding pipeline with pgvector, under 1M vectors postgres is sufficient
- [Zero-Shot Resume-Job Matching with LLMs (MDPI Electronics)](https://www.mdpi.com/2079-9292/14/24/4960) — structured prompting for job classification, 87% accuracy on zero-shot matching
- [Stale listings and job board freshness (Jobspikr)](https://www.jobspikr.com/blog/the-struggle-of-stale-listings-revitalize-your-job-board-with-job-scraping/) — staleness as a data quality problem; automated expiry patterns
- [LLM embedding caching for cost reduction (apxml.com)](https://apxml.com/courses/getting-started-with-llm-toolkit/chapter-9-performance-and-cost-optimization/caching-embeddings) — cache embeddings, only re-embed on content change
- [Google Cloud Talent Solution](https://cloud.google.com/solutions/talent-solution) — commercial benchmark for hard filters, soft scoring, location normalization

---

*Feature research for: Job scraping and matching engine (backend API)*
*Researched: 2026-03-25*
