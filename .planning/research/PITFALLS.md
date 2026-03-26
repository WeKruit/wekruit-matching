# Pitfalls Research

**Domain:** Job scraping + matching engine (GitHub README source, LLM enrichment, pgvector, weighted multi-signal scoring)
**Researched:** 2026-03-25
**Confidence:** HIGH (primary sources: GitHub changelog, pgvector GitHub issues, official pgvector docs, SimplifyJobs repo inspection, OpenAI/Anthropic API docs)

---

## Critical Pitfalls

### Pitfall 1: HTML Embedded in Markdown Table Cells Breaks Naive Parsers

**What goes wrong:**
The SimplifyJobs README uses `<details>/<summary>` HTML blocks inside table cells to collapse multi-location entries. A row like `<details><summary><strong>4 locations</strong></summary>London, UK<br>SF<br>NYC<br>Munich, Germany</details>` is valid GitHub Markdown but will confuse any parser that splits on `|` and treats cells as plain text. The parser sees raw HTML tags as location strings, or worse, splits incorrectly mid-tag and corrupts the row.

**Why it happens:**
Developers inspect a few rows manually, see clean text, and write a naive `line.split("|")` parser. They never test against multi-location rows. The SimplifyJobs repo also uses `↳` row continuations (same company, multiple roles) with no parent company field on those rows — requiring state-tracking across rows, which naive parsers skip.

**How to avoid:**
Use a proper Markdown parser (`mistune` or `markdown-it-py`) that handles inline HTML, then post-process cells to strip tags. Treat `↳` rows as inheriting the company from the prior non-continuation row. Write an explicit parser test using a downloaded snapshot of the live README that includes multi-location and continuation rows.

**Warning signs:**
- Location field contains raw `<details>` or `<br>` strings in your database
- Company field is empty for many rows (un-handled `↳` continuations)
- Row counts from parser are lower than visible rows in GitHub UI

**Phase to address:** Phase 1 (Scraper) — build the parser defensively from day one. Retrofitting a parser after enrichment has run is expensive (re-enrichment cost).

---

### Pitfall 2: GitHub Rate Limiting Kills Unauthenticated raw.githubusercontent.com Fetches

**What goes wrong:**
GitHub announced (May 2025) a rollout of stricter rate limits for unauthenticated requests, explicitly including `raw.githubusercontent.com` file downloads. Previously, fetching the README via `https://raw.githubusercontent.com/SimplifyJobs/...` was effectively unlimited for a single-instance scraper. Under the new limits, hitting this endpoint repeatedly without a token — especially during development, testing, and production cron runs — will result in 429 errors. The scraper silently returns an empty or truncated page and the caller may not notice.

**Why it happens:**
Scraping a raw README feels like a simple HTTP GET. Developers assume it's rate-limit-free because it's not the REST API. GitHub's changelog buried this change in infrastructure announcements.

**How to avoid:**
Always authenticate: use a GitHub personal access token (PAT) or GitHub App token in the `Authorization` header. Authenticated requests get 5,000 REST API req/hr (and substantially higher raw content limits). Alternatively, use the GitHub REST API `/repos/{owner}/{repo}/contents/{path}` endpoint with the `Accept: application/vnd.github.raw` header — authenticated, versioned, and with a clear error response when limits are hit. Add explicit 429 detection and exponential backoff.

**Warning signs:**
- HTTP 429 or 403 responses from `raw.githubusercontent.com` during cron
- Scraper returns empty content with no exception raised
- Cron works fine in development (low frequency) but fails in production (higher frequency)

**Phase to address:** Phase 1 (Scraper) — add token-authenticated fetching before any production deployment.

---

### Pitfall 3: pgvector Index Silently Falls Back to Sequential Scan

**What goes wrong:**
pgvector's HNSW or IVFFlat index is only used when the query includes `ORDER BY embedding <=> $1 LIMIT N` with the matching operator. If the WHERE clause filters too aggressively (few rows survive), or if the query uses `<->` (L2) while the index was built with `vector_cosine_ops` (cosine), PostgreSQL's planner abandons the index and performs a full sequential scan. At 1,000 jobs this is fine (milliseconds). At 50,000 jobs, it is 30+ seconds. No error is raised — queries return correct-looking results, just catastrophically slowly.

**Why it happens:**
Two failure modes:
1. **Operator mismatch**: Developer builds index with `vector_cosine_ops` then writes queries using `<->` (L2 distance). The index cannot be used. No warning.
2. **Aggressive pre-filtering**: Adding hard filters (`WHERE is_active = true AND job_type = 'internship'`) before the vector ORDER BY can reduce the candidate set to where the planner decides seq scan is cheaper.

**How to avoid:**
Standardize on one distance metric — cosine (`<=>`) for `text-embedding-3-small` embeddings (OpenAI models produce normalized vectors so inner product and cosine are equivalent, but pick one and never mix). Build the index with `vector_cosine_ops`. Verify the index is being used with `EXPLAIN ANALYZE` after every schema/query change. For filtered queries, use approximate filtering: apply hard filters in a post-processing step after fetching top-K vector candidates, not as a pre-filter in SQL.

**Warning signs:**
- `EXPLAIN ANALYZE` shows `Parallel Seq Scan` instead of `Index Scan`
- Query time spikes from <10ms to >1s as job count grows
- `pg_indexes` shows `vector_cosine_ops` but queries use `<->`

**Phase to address:** Phase 2 (Database + Embeddings) — validate index usage with `EXPLAIN ANALYZE` as part of the phase completion criteria.

---

### Pitfall 4: LLM Enrichment Cost Explosion on Every Scrape Run

**What goes wrong:**
The enrichment pipeline calls Claude (Anthropic) for each job to classify industry, company size, skills, and sponsorship. If the enrichment check is keyed on "job is in table" rather than "job fields have changed," every daily scrape re-enriches all jobs even when nothing changed. At 2,000 jobs and $0.01/job enrichment cost, a daily cron that re-enriches everything costs $20/day, $600/month — before embeddings.

**Why it happens:**
Simple `INSERT OR IGNORE` or "enrich if missing" logic works in testing where jobs are inserted once. In production, the upsert updates existing rows (fixing the Age column), which triggers re-enrichment in naive implementations that check `updated_at` rather than whether enrichable fields changed.

**How to avoid:**
Store a content hash of the source row fields that feed enrichment (company name + role title). Only re-enrich if this hash changes. Use the Anthropic Batch API (available for async workloads) for 50% cost reduction on bulk enrichment. Use Claude Haiku for enrichment tasks — it is significantly cheaper than Sonnet/Opus for structured classification tasks with well-designed prompts. Enrich new jobs immediately; re-enrich existing jobs only on content change.

**Warning signs:**
- API cost grows proportionally to total job count rather than to new-job count
- `enriched_at` timestamp updates daily for all jobs, not just new/changed ones
- Monthly Anthropic bill spikes after the scraper dataset grows

**Phase to address:** Phase 2 (Database + Enrichment) — design the upsert + re-enrichment trigger logic before running any enrichment at scale.

---

### Pitfall 5: Stable ID Generation Breaks on Company Name Variations

**What goes wrong:**
The system needs a stable job ID to deduplicate across scrape runs. If the ID is derived from `hash(company_name + role_title + location)`, any variation in how SimplifyJobs writes the company name — "Meta" vs "Meta Platforms" vs "🔥 Meta" (with the FAANG emoji prefix) — generates a different hash and creates duplicate entries. The same job gets enriched and embedded twice, and the stale version is never marked inactive.

**Why it happens:**
The lock emoji (`🔒`) for closed jobs and the FAANG emoji (`🔥`) are part of the Company field. Developers who strip emojis at display time but hash the raw field get hash instability whenever SimplifyJobs changes their emoji usage, which they do seasonally and at formatting updates.

**Why it matters here specifically:** The `↳` continuation rows present a secondary problem — they inherit the company from the prior row, but if a developer incorrectly creates them as standalone entries with `↳` as the company name, the hash function produces garbage IDs.

**How to avoid:**
Normalize before hashing: strip all emoji, lowercase, strip punctuation, collapse whitespace. Use `(normalized_company, normalized_role, primary_location)` as the composite natural key, not a hash of raw values. Store the normalized key as a generated column in Postgres for fast lookup. Test the ID function against the actual README, specifically on rows with emojis, `↳` continuations, and multi-location `<details>` blocks.

**Warning signs:**
- Same company/role combination appearing multiple times in the database with different IDs
- `is_active` never being set to False (deduplication key never matches on update)
- Total job count grows unbounded across scrape runs instead of stabilizing

**Phase to address:** Phase 1 (Scraper) — ID generation must be correct before enrichment runs, otherwise you pay double enrichment cost for every duplicate.

---

### Pitfall 6: Embedding Drift When the Embedding Model Changes

**What goes wrong:**
`text-embedding-3-small` embeddings stored today are mathematically incompatible with embeddings generated by a future model version. Cosine similarity between a v1 query embedding and a v2 document embedding can be as low as 0.78 — the index silently returns wrong results. This is a slow-onset failure: the system works fine until it doesn't, and the degradation is invisible unless you measure retrieval quality explicitly.

**Why it happens:**
Teams rarely plan for embedding model migration at project start. The immediate pressure is to ship. When OpenAI releases a better model, the upgrade path requires recomputing all stored embeddings, which is a batch operation that costs money and requires downtime (or dual-index management during transition).

**How to avoid:**
Store `embedding_model` and `embedding_model_version` alongside every embedding row. When these values are queried, compare with the currently configured model — any mismatch means stale embedding. Add a `needs_reembedding` flag to the job schema. At model upgrade time, run a bulk re-embedding job before switching the query path. For this project's scale (a few thousand jobs), a full recompute costs under $0.10 with batch API — the risk is not cost, it is forgetting to do it.

**Warning signs:**
- Match quality reports decline after model update
- Some users report irrelevant recommendations that were fine before
- `embedding_model` column has mixed values across rows

**Phase to address:** Phase 2 (Database + Embeddings) — add `embedding_model` column to schema on day one, not as a retrofit.

---

### Pitfall 7: Cold Start Produces Useless Matches for New Users

**What goes wrong:**
A new user provides a profile with a few skills and a vague preference. The matching engine has no feedback history, so `feedback_boost` contributes nothing and the affinity embedding is a zero vector. The system falls back to pure weighted scoring, which over-weights `title_similarity` (0.30 weight) and returns whatever job titles most exactly match the profile's target role — often highly competitive roles the user is unlikely to land, or generic SWE roles that ignore their actual interests.

**Why it happens:**
The weighted scoring weights (title 0.30, skills 0.25, etc.) were designed for users with some history. New users get the same weights, but several signals are degenerate: feedback_boost is 0, affinity embedding similarity is undefined or random. The result is a ranking dominated by whichever weights happen to fire for incomplete profiles.

**How to avoid:**
Explicitly detect new-user state: if `feedback_count < threshold` (e.g., 3), apply a cold-start mode that suppresses feedback_boost, explicitly asks for more profile signal (desired company size, must-have tech stack), and emphasizes recency (newer jobs = more likely still open, easier to apply to). Consider returning a deliberately broad top-K set on first query to gather feedback quickly. Document the feedback-count threshold as a tunable parameter, not a hardcoded magic number.

**Warning signs:**
- First-session match scores are clustered at similar values (no differentiation)
- New users click through to jobs and immediately leave (bounce signal)
- `feedback_boost = 0` for all returned results in new user sessions

**Phase to address:** Phase 3 (Matching Engine + Feedback) — design cold-start handling explicitly as a first-class concern during matching engine implementation.

---

### Pitfall 8: Feedback Loop Narrows Results Into a Filter Bubble

**What goes wrong:**
A user likes 10 FAANG software engineering internships. The affinity embedding drifts toward "large tech company SWE." The feedback_boost amplifies FAANG results. The user stops seeing relevant opportunities at startups or non-SWE roles (PM, data science) they might actually want. Over time, the matching engine self-reinforces preferences the user expressed early, not preferences they hold currently.

**Why it happens:**
Feedback systems are designed to improve relevance by learning from behavior. The failure is treating every feedback signal as equally valid indefinitely. Early feedback from an uninformed user (who hadn't seen many jobs yet) gets permanent weight in the affinity embedding.

**How to avoid:**
Apply feedback decay: weight recent feedback more heavily than old feedback using time-decay functions. Add an explicit diversity injection: ensure the top-K results always include at least 20% jobs outside the user's established affinity cluster (controlled exploration). Allow the user to explicitly reset preferences. Store feedback timestamps so decay can be applied retroactively when the scoring function is updated.

**Warning signs:**
- Result set entropy decreases monotonically (less diversity over sessions)
- User explicitly searches for a different company/role type but matches don't change
- All returned companies are the same 5-10 employers after 20+ feedback events

**Phase to address:** Phase 3 (Matching Engine + Feedback) — implement diversity injection from the start; it is much harder to add after users have feedback histories.

---

### Pitfall 9: LLM Hallucination in Structured Enrichment Fields

**What goes wrong:**
Claude classifies jobs for industry, company size, skills, and visa sponsorship. The Anthropic Structured Outputs feature guarantees valid JSON format — it does not guarantee correct content. Claude may confidently classify a "Quantitative Research Intern" at a hedge fund as "Software Engineering" industry, or mark a job as offering sponsorship when the posting is silent on the topic. These errors propagate to every user who would have benefited from that filter.

**Why it happens:**
Structured output guarantees format compliance, not factual accuracy. The job data in the SimplifyJobs README is terse — typically just company name, role title, and location. Claude must infer industry and company size from minimal signal, creating high hallucination risk on ambiguous cases.

**How to avoid:**
Add a confidence score to each enrichment field (returned by Claude alongside the classification). For `sponsorship_offered`, default to null (unknown) rather than false when the README row has no explicit signal — surface "unknown" to users as a separate filter state rather than silently excluding jobs. Use a two-pass enrichment: Claude classifies, then a deterministic post-processor validates against known company databases for company size. For skills extraction, limit to a controlled vocabulary rather than free-form tags to reduce hallucination surface area.

**Warning signs:**
- Users report being filtered out of jobs they know they're eligible for
- Sponsorship filter excludes significantly more jobs than expected given the source repo's known composition
- Company size classifications are inconsistent for the same employer across different role listings

**Phase to address:** Phase 2 (Enrichment) — add confidence scores and null-safe filter states before running enrichment at scale.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Raw string split on `\|` for markdown table parsing | Fast to write | Breaks on multi-location HTML cells, corrupts location data silently | Never — use a real parser from day one |
| No `embedding_model` column on jobs table | Simpler schema | Cannot detect stale embeddings after model upgrade; full recompute required with no way to identify stale rows | Never — add this column in the initial migration |
| Enrich all jobs on every scrape run | Simple logic | Cost explosion proportional to total job count, not new job count | Never — content hashing is cheap insurance |
| Unauthenticated GitHub raw fetch | No token management | 429 errors in production; new GitHub rate limits explicitly target this | Never in production |
| Hard WHERE filters before vector ORDER BY | Intuitive SQL structure | Forces sequential scan, defeats index entirely | Never — apply hard filters post-retrieval |
| Hardcode feedback weights as constants | Fast to ship | Cannot A/B test or tune without a redeploy | Acceptable in MVP; add config table in Phase 3 |
| Single IVFFlat index without tuning lists parameter | Zero config | Query optimizer abandons index at moderate scale | Acceptable in Phase 1 for <1,000 jobs; tune before scaling |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| GitHub raw README fetch | Unauthenticated HTTP GET to `raw.githubusercontent.com` | Authenticate with PAT in `Authorization: Bearer` header; add 429 detection |
| OpenAI Embeddings API | Embedding one job per API call | Batch up to 2,048 texts per request; use Batch API for 50% cost reduction on bulk runs |
| Anthropic Claude (enrichment) | Not using Batch API for initial bulk enrichment | Use Batch API for non-urgent async enrichment; use Haiku model not Sonnet for classification tasks |
| pgvector | Building HNSW index then using L2 operator in queries | Match index operator class to query operator: `vector_cosine_ops` with `<=>` exclusively |
| Postgres UPSERT | Triggering re-enrichment on every `ON CONFLICT DO UPDATE` | Track content hash of enrichable fields; only flag for re-enrichment when hash changes |
| SimplifyJobs README | Treating `Age` column as a parseable date | The `Age` column is a display string ("0d", "8d+"), not a timestamp; use GitHub commit metadata for actual posting timestamp |

---

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Sequential scan on vector column (no index or wrong operator) | Queries degrade from 5ms to 30s+ as job count grows | Use `EXPLAIN ANALYZE`; verify index usage after every schema change | ~5,000+ rows |
| Re-embedding all jobs on every cron run | Embedding API cost grows with total job count | Content hash check before re-embedding | From day one — cost is immediate |
| IVFFlat with wrong `lists` parameter | Index abandoned by planner for small tables; poor recall for large tables | Use `rows / 1000` for tables under 1M rows; set `lists` at index creation | Immediately on creation (wrong formula) |
| Fetching all jobs into memory before filtering | OOM in matching engine for large result sets | Use server-side SQL filtering for hard filters; only load top-K candidates | ~50,000+ jobs |
| `maintenance_work_mem` too low for index build | Index creation fails with OOM error | Set `maintenance_work_mem` to at least 256MB before HNSW index creation | During initial index build |

---

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Storing GitHub PAT in source code or `.env` committed to repo | Token theft exposes scraper's GitHub access | Use environment variables; never commit `.env`; use short-lived tokens |
| No validation of LLM-returned JSON before DB insert | Malformed enrichment data corrupts DB; SQL injection if enrichment output is interpolated into queries | Use parameterized queries always; validate all LLM output against schema before insert |
| Exposing raw user profile data in matching API logs | PII leakage | Log only user IDs, not profile contents; mask skills/preferences in debug logs |
| No rate limiting on matching API endpoint | Embedding API cost attack (caller can spam match requests, each triggering OpenAI call) | Cache query embeddings by profile hash; rate limit API consumers |

---

## "Looks Done But Isn't" Checklist

- [ ] **Scraper:** Handles `<details>/<summary>` multi-location HTML in table cells — verify by checking `location` field in DB contains no raw HTML tags
- [ ] **Scraper:** `↳` continuation rows correctly inherit company name — verify by checking that no job row has `↳` as its company value
- [ ] **Scraper:** Closed/locked jobs (`🔒`) are skipped at parse time, not filtered post-insert — verify by checking `is_active` logic and that locked emoji rows never enter enrichment queue
- [ ] **Deduplication:** `normalized_key` is consistent across scrape runs — verify by running scraper twice on same README snapshot and confirming zero new rows inserted
- [ ] **Enrichment:** Re-enrichment only fires when content hash changes — verify by running scraper twice and confirming no Anthropic API calls on the second run
- [ ] **Embeddings:** `embedding_model` column populated on every job row — verify with `SELECT COUNT(*) FROM jobs WHERE embedding_model IS NULL`
- [ ] **pgvector:** HNSW index is actually used by the query planner — verify with `EXPLAIN ANALYZE SELECT ... ORDER BY embedding <=> $1 LIMIT 10`
- [ ] **Matching:** Cold-start path triggers for new users — verify by running matching with a zero-feedback-history profile and checking that `feedback_boost` contribution is 0 and results are appropriately diverse
- [ ] **Sponsorship filter:** Null/unknown sponsorship jobs surface correctly — verify that `sponsorship_offered IS NULL` jobs appear when user filter is "include unknown"

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Duplicate jobs due to bad ID function | HIGH | Deduplicate DB, re-derive IDs from normalized keys, re-run enrichment for merged records |
| All embeddings stale after model upgrade | MEDIUM | Batch re-embed all active jobs via Batch API ($0.01/1M tokens); takes ~30min for 5,000 jobs |
| pgvector using sequential scan in production | LOW | Add/rebuild index with correct operator class; verify with EXPLAIN ANALYZE; no data loss |
| LLM enrichment over-billed due to re-enrichment bug | HIGH (cost already spent) | Fix content hash check; no way to recover past API spend |
| GitHub rate limit blocking production scraper | LOW | Add PAT authentication; rate limit was just exposing missing auth |
| Feedback bubble locked user into narrow results | MEDIUM | Add time-decay to feedback scoring; recompute affinity embeddings with decayed weights |

---

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| HTML in markdown table cells breaks parser | Phase 1: Scraper | Parser test against live README snapshot with multi-location rows |
| Unauthenticated GitHub fetch rate limited | Phase 1: Scraper | Scraper runs 100 consecutive fetches without 429 |
| Stable ID breaks on emoji / company name variations | Phase 1: Scraper | Dual-run deduplication test — same README, zero new rows on second run |
| `↳` continuation rows lose company name | Phase 1: Scraper | No job row in DB has company = `↳` or company = null |
| LLM re-enrichment on every scrape (cost explosion) | Phase 2: Enrichment | Scraper run on unchanged data produces zero Anthropic API calls |
| LLM hallucination in sponsorship / industry fields | Phase 2: Enrichment | Confidence scores present; null state used for unknown sponsorship |
| No `embedding_model` column | Phase 2: Embeddings | Schema migration includes `embedding_model VARCHAR NOT NULL` |
| pgvector operator class mismatch | Phase 2: Embeddings | `EXPLAIN ANALYZE` shows Index Scan, not Seq Scan |
| Embedding drift after model upgrade | Phase 2: Embeddings | `embedding_model` column enables targeted recompute; documented runbook exists |
| Cold start poor matches | Phase 3: Matching | Cold-start profile returns diverse results; no single company dominates |
| Feedback loop filter bubble | Phase 3: Matching | After 20 feedback events, result diversity metric (entropy) remains above threshold |

---

## Sources

- GitHub Changelog: "Updated rate limits for unauthenticated requests" (May 8, 2025) — https://github.blog/changelog/2025-05-08-updated-rate-limits-for-unauthenticated-requests/
- pgvector GitHub: HNSW index not used for KNN queries issue #835 — https://github.com/pgvector/pgvector/issues/835
- pgvector GitHub: Cosine distance vs cosine similarity (issue #72) — https://github.com/pgvector/pgvector/issues/72
- Crunchy Data: pgvector performance for developers — https://www.crunchydata.com/blog/pgvector-performance-for-developers
- pgvector GitHub: Open-source vector similarity search for Postgres — https://github.com/pgvector/pgvector
- AWS Blog: pgvector indexing deep dive (IVFFlat and HNSW) — https://aws.amazon.com/blogs/database/optimize-generative-ai-applications-with-pgvector-indexing-a-deep-dive-into-ivfflat-and-hnsw-techniques/
- Anthropic Docs: Structured outputs — https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- OpenAI Docs: Vector embeddings and batch API — https://developers.openai.com/api/docs/guides/embeddings
- Medium: "When Embeddings Go Stale: Detecting & Fixing Retrieval Drift in Production" — https://medium.com/@yashtripathi.nits/when-embeddings-go-stale-detecting-fixing-retrieval-drift-in-production-778a89481a57
- Zilliz: "What is embedding drift and how do I detect it?" — https://zilliz.com/ai-faq/what-is-embedding-drift-and-how-do-i-detect-it
- SimplifyJobs Summer2026-Internships README (live inspection) — https://github.com/SimplifyJobs/Summer2026-Internships/blob/dev/README.md
- Supabase GitHub issue: "pgvector <=> is cosine distance, not cosine similarity" — https://github.com/supabase/supabase/issues/12244
- ScrapingAnt: "Building a Web Data Quality Layer" — https://scrapingant.com/blog/building-a-web-data-quality-layer-deduping-canonicalization
- Frontiers in AI: "Explainable person-job recommendations: challenges" (2025) — https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2025.1660548/full

---
*Pitfalls research for: job scraping + matching engine (SimplifyJobs GitHub source, Postgres/pgvector, LLM enrichment)*
*Researched: 2026-03-25*
