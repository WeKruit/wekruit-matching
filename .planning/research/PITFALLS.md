# Pitfalls Research

**Domain:** Job scraping + matching engine (GitHub README source, LLM enrichment, pgvector, weighted multi-signal scoring) — EXTENDED for Firecrawl scraping, ATS page parsing, and search milestone
**Researched:** 2026-03-31 (updated; original 2026-03-25)
**Confidence:** HIGH (primary sources: GitHub issues, Firecrawl official docs, pgvector GitHub issues, Supabase official docs, SimplifyJobs repo inspection, OpenAI/Anthropic API docs)

---

## PART A — NEW MILESTONE PITFALLS (Firecrawl, ATS Scraping, Pipeline Integration)

Pitfalls specific to adding Firecrawl scraping, ATS page parsing, and search to the existing 76K-job pipeline.

---

### Pitfall A1: Firecrawl Timeout Unit Bug Silently Disables Client-Side Timeout

**What goes wrong:**
The Firecrawl Python SDK `scrape_url()` method documents `timeout` as milliseconds, but the SDK passes the value directly to `requests.post()`, which expects seconds. Setting `timeout=60000` (intending 60 seconds) passes 60,000 seconds (~16.6 hours) to the underlying HTTP library — the scrape request blocks indefinitely with no client-side timeout. On the Mac Mini running launchd jobs, this means a hung scrape worker holds a Postgres connection open and blocks the enrichment stage from starting.

**Why it happens:**
The bug exists in the SDK internals. Developers trust SDK parameter names and never test edge cases where the scrape target is slow or unresponsive. The launchd wrapper has no process-level timeout by default.

**How to avoid:**
Pin a known-good `firecrawl-py` version that has addressed this issue. Always wrap Firecrawl calls with an `asyncio.wait_for()` or `httpx.Timeout` at the application level, independent of the SDK's timeout parameter. Set `StartTimeout` and `ExitTimeout` keys in your launchd plist. Use `timeout=30` (30 seconds) at the call site, treating it as seconds, and add a separate asyncio-level timeout of 45s as backstop. Reference: GitHub issue #1848 (firecrawl/firecrawl).

**Warning signs:**
- Scraping worker is still running 10+ minutes after launch
- Postgres connection pool exhausted (all connections held by hung scrape workers)
- launchd shows the scrape job still active when enrichment cron fires

**Phase to address:** Firecrawl integration phase — set `ExitTimeout` in launchd plist and add asyncio-level timeout wrappers on day one.

---

### Pitfall A2: Firecrawl Credits Are Not One-Per-Request (5x Multiplier on Extraction)

**What goes wrong:**
Firecrawl charges 1 credit per page scrape, but 5 credits per page when using the AI-powered `extract` feature. If you design an enrichment pipeline that calls Firecrawl's `extract` to pull structured fields from ATS job pages, your effective credit budget is one-fifth what you planned. At 1,000 ATS pages/day with extract, you consume 5,000 credits/day — not 1,000. Additionally, unused credits expire at the end of the billing cycle with no rollover.

**Why it happens:**
Firecrawl's pricing page prominently shows the per-page credit count; the multiplier for extract is in fine print. The dual pricing structure (credits for scrape, separate token-based subscription for AI extract) is not obvious from the main pricing table.

**How to avoid:**
Use Firecrawl's `scrape` endpoint (1 credit/page) to get raw markdown, then route the markdown through your existing SiliconFlow LLM pipeline for structured extraction — the enrichment stage you already built. Do not use Firecrawl's `extract` feature; it duplicates work you already pay for elsewhere and burns 5x credits. Only use `extract` for exploratory research; never in the production pipeline.

**Warning signs:**
- Monthly Firecrawl bill is 5x higher than projected from page counts
- Credit balance depletes mid-month on a predictable scrape volume that should fit the plan
- `usage.credits_used` per call is 5, not 1

**Phase to address:** Firecrawl integration phase — architecture decision: scrape-only, not extract; route markdown to existing LLM pipeline.

---

### Pitfall A3: Firecrawl Batch Jobs Get Stuck "Scraping" Without Completing

**What goes wrong:**
Firecrawl's async `start_batch_scrape()` returns a job ID. Polling `get_batch_scrape_status()` can show status `"scraping"` indefinitely — documented user reports show jobs stuck for 12+ hours past expected completion in the community discussion board. On a Mac Mini with a once-daily launchd schedule, a stuck batch job blocks the entire enrichment cron run, resulting in a day of no data with no alert raised.

**Why it happens:**
Firecrawl's batch queue is shared infrastructure. When upstream browser capacity is exceeded, jobs queue silently. If the queue threshold is exceeded, new requests receive 429, but jobs already enqueued may hang waiting for browsers to free. The SDK's `batch_scrape()` (blocking waiter) respects a `max_wait_time` parameter, but only stops auto-pagination — it doesn't cancel the underlying job.

**How to avoid:**
Never use blocking `batch_scrape()` in cron-driven scripts. Use `start_batch_scrape()` to get a job ID, store the ID in Postgres with a `started_at` timestamp, and poll on a separate shorter interval. Add a maximum age check: if a batch job is older than 90 minutes and still not `completed`, cancel it with `cancel_batch_scrape()` and log the failure. Implement alerting on failed/timed-out scrape jobs so the daily run failure is visible.

**Warning signs:**
- `get_batch_scrape_status()` returns `status: "scraping"` for more than 60 minutes
- Enrichment cron fails with "no new jobs to enrich" when new ATS pages should have been scraped
- Firecrawl activity log shows jobs in "queued" state from prior day

**Phase to address:** Firecrawl integration phase — job ID persistence + maximum age timeout are mandatory, not optional enhancements.

---

### Pitfall A4: ATS Platform Structures Diverge and Change Without Notice

**What goes wrong:**
Each ATS has a different URL scheme and data structure. Greenhouse exposes a public JSON API (`boards-api.greenhouse.io/v1/boards/{token}/jobs`) that is stable and authenticated-optional. Lever exposes a REST API (`api.lever.co/v0/postings/{company}`) that returns clean JSON. Workday uses a non-public internal CXS API (`{tenant}.{wd_server}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs`) that requires POST with JSON body — and the `wd_server` suffix (wd1, wd3, wd5, etc.) varies per company. A parser that works for `wd3` breaks for a company on `wd5`. Worse, the Workday server suffix is not derivable from the company name; it must be scraped from the company's career page first.

**Why it happens:**
Developers discover the CXS endpoint from community GitHub repos, copy the URL pattern, and assume wd3 is universal. The per-company server discovery step is skipped because it requires an extra HTTP call.

**How to avoid:**
For Greenhouse: use the official Job Board API, not HTML scraping. It's public, JSON, stable, and has no anti-scraping protections. For Lever: use `api.lever.co/v0/postings/{company}`. For Workday: implement a two-step fetch — (1) fetch the company's careers page HTML to extract the actual CXS endpoint URL, (2) POST to that URL. Never hardcode a Workday server suffix. Maintain an ATS type registry mapping company identifiers to detected ATS types and known endpoint URLs, refreshed monthly.

**Warning signs:**
- Workday scrapes fail for some companies but not others with no pattern
- ATS page fetch returns 404 after a company migrates to a different ATS
- HTTP 301 redirects to a completely different domain (company changed ATS provider)

**Phase to address:** ATS scraping phase — ATS type detection and endpoint discovery must precede batch scraping.

---

### Pitfall A5: Workday's CXS API Is Protected by Cloudflare and Requires Non-Trivial Headers

**What goes wrong:**
Workday career pages for many enterprise employers are protected by Cloudflare Bot Management (TLS fingerprinting, JavaScript challenges, behavioral analysis). A plain `httpx` GET to the careers page returns a Cloudflare challenge page (HTTP 403 or a JavaScript interstitial), not the actual page content. Firecrawl handles this for HTML scraping, but your direct calls to the CXS API endpoint with `httpx` will hit the same Cloudflare rules if the employer enables bot protection on their entire domain.

**Why it happens:**
Developers successfully scrape the CXS endpoint for a few employers (those without Cloudflare protection) and assume the approach is universal. Enterprise employers (banks, consulting firms, government contractors) are significantly more likely to have Cloudflare Bot Management enabled.

**How to avoid:**
Route Workday CXS API calls through Firecrawl's `scrape` endpoint instead of direct `httpx` calls — Firecrawl's headless browser infrastructure handles the TLS fingerprint and JavaScript challenges. For employers where Firecrawl also fails, fall back to the simplest effective strategy: check if the company posts to Greenhouse or Lever instead (many companies use multiple ATS paths). Do not attempt to bypass Cloudflare with custom TLS libraries in production; it violates ToS and Cloudflare continuously evolves detection.

**Warning signs:**
- HTTP 403 or 503 response from `myworkdayjobs.com` subdomains
- Response body contains "Checking if the site connection is secure" text
- Works in development (low frequency) but fails in production cron (higher frequency triggers bot detection)

**Phase to address:** ATS scraping phase — test with at least 3 enterprise employer Workday pages before committing to direct CXS API approach; budget for Firecrawl route for blocked domains.

---

### Pitfall A6: `__NEXT_DATA__` Scraping Breaks When Sites Migrate to Next.js App Router

**What goes wrong:**
The `__NEXT_DATA__` JSON blob embedded in Next.js Pages Router HTML is a common shortcut for extracting structured data from job boards (JobRight and similar) without parsing HTML. This technique works for Pages Router (`/pages/` directory). When a site migrates to the App Router (Next.js 13+), there is no `__NEXT_DATA__` blob; data is fetched server-side via React Server Components and streaming flight protocol. The scraper silently returns an empty dict when `__NEXT_DATA__` is absent, and no exception is raised.

**Why it happens:**
`__NEXT_DATA__` extraction is widely documented in scraping guides as a reliable technique for Next.js sites. Guides from 2021-2023 do not cover the App Router migration that became mainstream in 2023-2024. Sites migrated to App Router without announcing it publicly.

**How to avoid:**
Never rely solely on `__NEXT_DATA__` extraction. Add a presence check: if `window.__NEXT_DATA__` is absent in the response HTML, fall back to DOM parsing or route through Firecrawl's headless browser. Treat `__NEXT_DATA__` as an opportunistic fast path, not a guaranteed structure. Set up a monitoring job that detects when `__NEXT_DATA__` disappears from a previously reliable source and pages an alert.

**Warning signs:**
- `__NEXT_DATA__` extraction returns empty dict or raises KeyError where it previously worked
- Response HTML size drops significantly (Pages Router sends full SSR; App Router sends a shell)
- The page has a `<script type="text/x-next-data-opts">` or no `<script id="__NEXT_DATA__">` at all

**Phase to address:** ATS/job board scraping phase — `__NEXT_DATA__` extraction must have a fallback path and presence assertion.

---

### Pitfall A7: Supabase Statement Timeout Kills Large Batch Upserts

**What goes wrong:**
You already hit this. Supabase enforces per-role statement timeouts: `anon` role gets 3 seconds, `authenticated` role gets 8 seconds, and even the `postgres` role is capped at 2 minutes by a global timeout. A batch upsert of 33K rows in one transaction exceeds these limits and gets cancelled with `ERROR: canceling statement due to statement timeout`. The partially-written batch leaves the jobs table in an inconsistent state — some rows enriched, some not — and the scraper will re-attempt the same rows on the next run, triggering duplicate-enrichment risk if content hashes aren't correctly preserved.

**Why it happens:**
The existing pipeline was designed for incremental daily updates (hundreds of new rows), not bulk initial loads or large catch-up batches. New ATS sources add large one-time imports of job pages that exceed incremental assumptions.

**How to avoid:**
Cap batch size at 500 rows per transaction, not 33K. Implement chunked upserts with commit after each chunk: `for chunk in chunks(jobs, size=500): upsert(chunk); conn.commit()`. For initial bulk loads from new ATS sources, use a dedicated session with `SET statement_timeout = 0` (disabled) via direct Postgres connection (port 5432 session mode), not through the Supabase pooler (which ignores per-session timeout overrides). Never use the Supabase REST API client (`supabase-py`) for bulk inserts — it hits the 8-second authenticated role limit immediately.

**Warning signs:**
- `ERROR: canceling statement due to statement timeout` in scraper logs
- Job count in DB is lower than expected after a bulk import run
- `enriched_at IS NULL` jobs appear that have a `created_at` from a prior run (partial batch succeeded on a previous attempt)

**Phase to address:** ATS/data ingestion phase — chunked upserts are mandatory; set 500 as the hard batch-size constant from day one.

---

### Pitfall A8: SiliconFlow 1K RPM / 50K TPM Limits Both Fire Independently

**What goes wrong:**
SiliconFlow rate limits trigger on whichever metric (RPM or TPM) hits its cap first. With 1,000 RPM and 50,000 TPM: if you send 50 requests per minute, each with 1,000 tokens, you hit the TPM cap (50,000 tokens) after exactly 50 requests — your RPM is 950 below limit, but you're blocked. With short prompts (~200 tokens each), you can hit the RPM cap (1,000 RPM) while consuming only 200,000 tokens/min — well under the token limit. The effective throughput depends on prompt size, and naive backoff code that only handles 429 from RPM may not correctly distinguish RPM vs TPM exhaustion.

**Why it happens:**
Developers test with a small batch, measure RPM headroom, and incorrectly assume token headroom scales proportionally. The two limits interact non-linearly based on prompt length distribution.

**How to avoid:**
At the start of each enrichment batch, estimate total tokens: `len(jobs) * avg_tokens_per_prompt`. If `estimated_tokens > 45_000` (90% of TPM), chunk the batch into time-separated segments with a 60-second sleep between. Track both `x-ratelimit-remaining-requests` and `x-ratelimit-remaining-tokens` response headers; consume whichever is lower to determine safe next-request timing. Write a `RateLimiter` class that tracks both dimensions independently, not just 429 count.

**Warning signs:**
- 429 errors after far fewer than 1,000 requests in a minute
- 429 rate is higher when scraping detailed job descriptions (longer prompts) vs short terse listings
- Enrichment throughput inconsistent across runs with the same job count

**Phase to address:** LLM enrichment integration phase — dual-dimension rate limiter required before SiliconFlow is used in production pipeline.

---

### Pitfall A9: New Scraping Stage Corrupts Existing `content_hash` If Schema Misaligned

**What goes wrong:**
The existing pipeline uses `content_hash` (SHA-256 of company + role + location) as the enrichment gate: if hash unchanged, skip enrichment. When you add ATS page scraping, you may enrich the `job_description` field with richer text from the actual ATS page. If `content_hash` is not updated to include the ATS-derived `job_description`, the hash will never change when the ATS page updates its description, and re-enrichment will never trigger. Conversely, if you naively add `job_description` to the hash input, every row's hash changes on first ATS scrape — triggering a full re-enrichment of all 76K jobs and a corresponding LLM bill.

**Why it happens:**
The content hash was designed for the GitHub README source. Adding a new data source with richer fields requires rethinking what "content changed" means per-source. Developers add the new field to the hash without considering the migration cost of the first run.

**How to avoid:**
Add a separate `ats_content_hash` column that tracks changes to ATS-derived fields independently from the GitHub README `content_hash`. Gate ATS-specific re-enrichment on `ats_content_hash` changes. The first ATS scrape populates `ats_content_hash` for all rows; subsequent scrapes only re-enrich when that hash changes. Never modify the existing `content_hash` logic — it is working correctly for the GitHub source.

**Warning signs:**
- After deploying ATS scraping, an enrichment run touches every job in the table
- Enrichment runs cost 10x more than normal on the first day after ATS scraping goes live
- `enriched_at` timestamps reset to current time for all jobs, not just newly scraped ones

**Phase to address:** Data integration phase — schema migration adding `ats_content_hash` before ATS scraping goes live.

---

### Pitfall A10: HNSW Index Degrades 100x for Non-Vector Column Updates

**What goes wrong:**
A confirmed pgvector issue (GitHub issue #875): updating non-vector columns (e.g., `is_active`, `enriched_at`, `job_description`, `status`) on a table with an HNSW index is catastrophically slow — approximately 100x slower than without the index. Updating 10,000 rows takes ~6 seconds with the HNSW index vs ~58 milliseconds without it. This affects the existing upsert pipeline today, but becomes significantly worse when ATS scraping adds bulk non-vector updates (job description updates, status changes, close detection) to the 76K-row jobs table.

**Why it happens:**
The HNSW index unnecessarily triggers buffer access during non-vector column updates — a known pgvector implementation issue. The maintainer's recommended fix is a schema split.

**How to avoid:**
The official recommendation is to split the table: one table with only the vector column + foreign key (receives HNSW index), one table with all non-vector frequently-updated columns. For this pipeline, the minimum viable fix is to batch all non-vector updates in a single transaction rather than row-by-row, and to run non-vector updates separately from vector inserts. In the new ATS scraping stage, never update a job row's `job_description` in the same upsert that touches the `embedding` column.

**Warning signs:**
- Upsert runs that worked in <10s now take several minutes after ATS scraping adds bulk updates
- `EXPLAIN ANALYZE` on non-vector UPDATEs shows high buffer access counts relative to row count
- Pipeline stage times grow proportionally with total table size, not new-row count

**Phase to address:** ATS data integration phase — separate vector and non-vector update operations; consider table split if update frequency increases significantly.

---

### Pitfall A11: Stale and Ghost ATS Listings Degrade Match Quality

**What goes wrong:**
An estimated 30-40% of job postings on external ATS pages represent positions that are already filled, on hold, or never genuinely open ("ghost postings"). Unlike the SimplifyJobs GitHub README — which marks closed jobs with a lock emoji — ATS boards (especially Workday) often leave filled positions visible for weeks or months after closure. Scraping and enriching these stale listings poisons the matching engine: users get matched to jobs they cannot apply to, and the LLM wastes tokens enriching dead listings.

**Why it happens:**
ATS boards are employer-facing tools. Removing a listing requires deliberate action; many employers forget or deprioritize removing filled positions. There is no universal "closed" signal in Workday/Greenhouse/Lever JSON responses — some use `status: "closed"`, others use `status: "live"` for open and simply remove the listing (404) when closed.

**How to avoid:**
Treat 404 on a previously-scraped ATS URL as a strong "closed" signal and mark `is_active = false` immediately. For Greenhouse, use `updated_at` from the Job Board API — jobs not updated in 90+ days are very likely stale. Set a freshness TTL: any ATS-scraped job not confirmed live in the last 30 days should have `is_active = false` regardless of what the page says. Explicitly check for `status` fields in Greenhouse and Lever JSON; map `"closed"` to `is_active = false` at parse time.

**Warning signs:**
- Users click "Apply" links from matched jobs and receive 404 or "position filled" pages
- ATS job count does not decrease over time despite jobs presumably being filled
- Date distribution of ATS jobs is heavily skewed toward past months (many old listings)

**Phase to address:** ATS scraping phase — TTL and 404 detection are table-stakes, not optional quality improvements.

---

### Pitfall A12: Adding New Scraping Stages Without Feature-Flag Isolation Breaks the Working Pipeline

**What goes wrong:**
The existing GitHub README scraper is working in production. Adding Firecrawl-based ATS scraping as a new code path in the same scraper module risks introducing import errors, connection pool conflicts, or exception handling changes that break the existing scraper. A bug in the ATS scraping stage can propagate upstream and prevent the GitHub scrape from running.

**Why it happens:**
Developers extend the existing scraper module because it already has the DB connection pool and upsert logic. The new code runs in the same process as the old code, sharing resources. A `TypeError` in the Firecrawl client initialization causes the entire scraper module to fail on import.

**How to avoid:**
Implement new ATS scraping as a separate script/module with its own entrypoint (`scripts/ats_scraper.py`), separate launchd plist, and independent error boundary. The existing `cron_scraper.sh` must not be modified — do not add new scraping sources to it. The new ATS scraper should be additive-only: it writes to a staging table or uses a source-tagged upsert path. The GitHub README scraper continues to run on its existing schedule, unchanged. Only merge their data at the enrichment stage via a unified view or fan-in query.

**Warning signs:**
- GitHub scraper fails to start after ATS scraper code is deployed
- Postgres connection pool errors appear during the GitHub scraper run after the new module is introduced
- Import errors in `wekruit_matching` package after adding new dependencies (e.g., `firecrawl-py`)

**Phase to address:** Architecture decision before any ATS scraping code is written — separate modules, separate entrypoints, shared DB only at the upsert layer.

---

### Pitfall A13: Encoding and HTML Entity Contamination From ATS Pages Poisons Embedding Input

**What goes wrong:**
ATS job pages (especially Workday HTML descriptions and Greenhouse rich text bodies) contain HTML entities (`&amp;`, `&lt;`, `&nbsp;`), Unicode smart quotes (`\u2019`, `\u201c`), soft hyphens (`\xad`), and zero-width spaces (`\u200b`). These characters pass through Firecrawl's markdown conversion but survive as literal text. When this text is fed to the OpenAI embedding model, the embedding for "5+ years experience" (with smart apostrophe) is not the same as "5+ years experience" (with ASCII apostrophe). Skill overlap matching that compares extracted skills from terse GitHub listings against enriched ATS descriptions will produce systematically lower scores due to invisible character differences.

**Why it happens:**
Firecrawl's markdown output is clean-looking but not normalized. Developers inspect the output visually and see readable text; the invisible characters only show up in hex dumps or when debugging embedding distance anomalies.

**How to avoid:**
Add a mandatory text normalization step in the ATS parsing pipeline: (1) run `html.unescape()` on all text fields, (2) apply `unicodedata.normalize("NFKC", text)` to normalize Unicode variants to canonical forms, (3) strip zero-width characters (`\u200b`, `\u200c`, `\xad`), (4) normalize whitespace. This normalizer should be a standalone utility function tested against known ATS HTML patterns. Apply it at the scrape output stage, before any field is written to the DB.

**Warning signs:**
- Job descriptions contain `&amp;` or `&#160;` literal strings in the database
- Two identical-looking job descriptions have different content hashes (invisible character difference)
- Skill overlap scores are lower-than-expected for ATS-sourced jobs vs GitHub-sourced jobs

**Phase to address:** ATS parsing phase — normalization utility is a prerequisite before any ATS content reaches the embedding pipeline.

---

## PART B — ORIGINAL MILESTONE PITFALLS (retained for completeness)

The following pitfalls were documented in the original v1.0 milestone research (2026-03-25). They remain relevant and their prevention phases have been completed.

---

### Pitfall B1: HTML Embedded in Markdown Table Cells Breaks Naive Parsers

**What goes wrong:**
The SimplifyJobs README uses `<details>/<summary>` HTML blocks inside table cells for multi-location entries. A naive `line.split("|")` parser sees raw HTML tags as location strings or splits incorrectly mid-tag and corrupts the row.

**How to avoid:**
Use a proper Markdown parser that handles inline HTML, then post-process cells to strip tags. Treat `↳` rows as inheriting the company from the prior non-continuation row.

**Phase to address:** Phase 2 (Scraper) — completed 2026-03-26.

---

### Pitfall B2: GitHub Rate Limiting Kills Unauthenticated raw.githubusercontent.com Fetches

**What goes wrong:**
GitHub (May 2025) introduced stricter rate limits for unauthenticated raw content fetches. The scraper silently returns empty/truncated content on 429 with no exception.

**How to avoid:**
Always authenticate with a GitHub PAT. Add explicit 429 detection and exponential backoff.

**Phase to address:** Phase 2 (Scraper) — completed 2026-03-26.

---

### Pitfall B3: pgvector Index Silently Falls Back to Sequential Scan

**What goes wrong:**
If the WHERE clause pre-filters too aggressively, or the query uses the wrong distance operator for the index operator class, the planner abandons the index. At 76K jobs, this is a 30+ second query.

**How to avoid:**
Standardize on `<=>` (cosine) with `vector_cosine_ops` index. Apply hard filters post-retrieval, not as SQL pre-filters. Verify with `EXPLAIN ANALYZE`.

**Phase to address:** Phase 4 (Embeddings) — completed 2026-03-26.

---

### Pitfall B4: LLM Enrichment Cost Explosion on Every Scrape Run

**What goes wrong:**
Naive re-enrichment on every upsert triggers API calls for unchanged jobs. At 76K jobs, this is catastrophic.

**How to avoid:**
Content hash gate: only re-enrich when hash changes. Use Claude Haiku, not Sonnet/Opus.

**Phase to address:** Phase 3 (LLM Enrichment) — completed 2026-03-26.

---

### Pitfall B5: Stable ID Generation Breaks on Company Name Variations

**What goes wrong:**
Emojis, formatting changes, and `↳` continuation rows in SimplifyJobs README cause hash instability and duplicate entries.

**How to avoid:**
Normalize before hashing: strip emoji, lowercase, collapse whitespace.

**Phase to address:** Phase 2 (Scraper) — completed 2026-03-26.

---

### Pitfall B6: Embedding Drift When the Embedding Model Changes

**What goes wrong:**
Embeddings from different model versions are mathematically incompatible. Cosine similarity silently degrades.

**How to avoid:**
Store `embedding_model` on every row. Set `needs_reembedding` flag when model changes.

**Phase to address:** Phase 4 (Embeddings) — completed 2026-03-26.

---

### Pitfall B7: Cold Start Produces Useless Matches for New Users

**What goes wrong:**
New users get `feedback_boost = 0` and degenerate affinity embedding. Matches are dominated by title similarity at 0.30 weight.

**How to avoid:**
Explicit cold-start mode when `feedback_count < threshold`. Suppress `feedback_boost`. Return a broad top-K on first query.

**Phase to address:** Phase 6 (Scoring Engine) — completed 2026-03-26.

---

### Pitfall B8: Feedback Loop Narrows Results Into a Filter Bubble

**What goes wrong:**
Early feedback permanently dominates affinity embedding. Results converge to 5-10 employers.

**How to avoid:**
Time-decay on feedback. Diversity injection (20% results outside affinity cluster). Feedback timestamp storage for retroactive decay.

**Phase to address:** Phase 7 (Feedback Loop) — completed 2026-03-26.

---

### Pitfall B9: LLM Hallucination in Structured Enrichment Fields

**What goes wrong:**
Claude generates plausible-but-wrong classifications for terse listings with only company name + role title.

**How to avoid:**
Confidence scores per field. Default `sponsorship_offered = null` (unknown) when no explicit signal. Controlled skills vocabulary.

**Phase to address:** Phase 3 (LLM Enrichment) — completed 2026-03-26.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Use Firecrawl `extract` for ATS structured data | Avoids building a parser | 5x credit cost vs `scrape` + existing LLM pipeline | Never in production pipeline |
| Blocking `batch_scrape()` in cron scripts | Simpler code | Hung process blocks launchd pipeline stages | Never — always use `start_batch_scrape()` + async poll |
| Add ATS scraping to existing scraper module | Reuse connection pool | One bug breaks the working GitHub scraper | Never — separate modules, separate entrypoints |
| Hardcode Workday server suffix (wd3) | Fast to write | Fails for companies on wd1, wd5 | Never — discover from careers page |
| Skip text normalization before embedding | Faster scrape pipeline | Invisible Unicode differences degrade skill overlap scores | Never — normalization is cheap |
| Single content_hash covering GitHub + ATS fields | Simpler schema | Adding ATS fields to hash triggers full re-enrichment of 76K jobs | Never — separate `ats_content_hash` column |
| Direct `httpx` to Workday CXS API | No Firecrawl credits spent | Blocked by Cloudflare for enterprise employers | Acceptable for employers without Cloudflare; route blocked ones through Firecrawl |
| Raw string split on `\|` for markdown table parsing | Fast to write | Breaks on multi-location HTML cells | Never — already burned on this |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Firecrawl Python SDK | Pass `timeout=60000` (intending ms) | Treat SDK timeout as seconds; add asyncio-level timeout wrapper independently |
| Firecrawl batch jobs | Use blocking `batch_scrape()` in cron | Use `start_batch_scrape()`, store job ID in DB, poll with 90-min max-age check |
| Firecrawl `extract` feature | Use it for job field extraction | Use `scrape` (1 credit) + existing SiliconFlow LLM pipeline; extract costs 5 credits |
| Greenhouse Job Board API | Scrape HTML from board.greenhouse.io | Use `boards-api.greenhouse.io/v1/boards/{token}/jobs` — public JSON, no auth needed |
| Lever Postings API | Scrape HTML from jobs.lever.co | Use `api.lever.co/v0/postings/{company}` — public REST API, no auth needed |
| Workday CXS API | Hardcode `wd3.myworkdayjobs.com` | Discover actual server suffix from company's careers page HTML first |
| Supabase large batches | Single transaction for 33K rows | Chunk at 500 rows; SET statement_timeout = 0 for bulk loads via direct connection |
| SiliconFlow rate limits | Only handle RPM, ignore TPM | Track both `x-ratelimit-remaining-requests` and `x-ratelimit-remaining-tokens` |
| `__NEXT_DATA__` extraction | Treat as guaranteed structure | Presence-check first; fall back to Firecrawl headless if absent |
| GitHub raw README fetch | Unauthenticated HTTP GET | Authenticate with PAT; add 429 detection with exponential backoff |
| OpenAI Embeddings API | One embed call per job | Batch up to 2,048 texts; use Batch API for 50% cost on bulk runs |

---

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| HNSW index on non-vector UPDATE (pgvector #875) | 100x slower UPDATEs on `is_active`, `enriched_at`, `job_description` | Separate vector and non-vector updates; batch non-vector UPDATEs | Immediately with ATS bulk updates on 76K rows |
| Sequential scan on vector column (wrong operator) | Queries degrade from 5ms to 30s+ | `EXPLAIN ANALYZE`; `<=>` matches `vector_cosine_ops` index | ~5,000+ rows |
| Single 33K-row upsert transaction | Statement timeout kills batch | Chunk at 500 rows per transaction | Immediately on Supabase with >8s query time |
| SiliconFlow dual-limit exhaustion | 429 after fewer than 1K requests/min | Track both RPM and TPM remaining headers | Any batch with >50K tokens/min throughput |
| Re-embedding all jobs after content_hash change | $$$, hours of OpenAI calls | Separate `ats_content_hash`; only re-embed rows whose source hash changed | First ATS scrape run if hash design is wrong |
| Fetching all jobs into Python before filtering | OOM on 76K+ jobs | Server-side SQL filtering; load only top-K candidates | ~50,000 jobs already in pipeline |

---

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Storing Firecrawl API key in source code | Key theft enables credit drain | Environment variable; Infisical; never commit |
| Storing SiliconFlow API key in source code | Key theft enables LLM cost abuse | Environment variable; Infisical; rate-limit monitoring |
| Scraping ATS pages that disallow scrapers in robots.txt | ToS violation; IP ban; legal risk | Check robots.txt before adding any new source; prefer official JSON APIs (Greenhouse, Lever) |
| Logging raw Firecrawl response bodies in DEBUG | Job description content in logs; potential PII (recruiter contact info) | Log only status codes and byte counts; never log response bodies |
| No rate limiting on matching API endpoint | Caller can trigger unlimited OpenAI embedding calls | Cache query embeddings by profile hash; rate limit API consumers |

---

## "Looks Done But Isn't" Checklist

**New milestone additions:**

- [ ] **Firecrawl timeout:** Application-level asyncio timeout wrapper exists independently of SDK timeout parameter — verify by testing with an intentionally slow URL
- [ ] **Firecrawl credits:** Pipeline uses `scrape` (not `extract`) for all ATS page fetches — verify that `usage.credits_used` per call is 1, not 5
- [ ] **Batch job persistence:** Firecrawl batch job IDs stored in DB with `started_at`; max-age check at 90 minutes — verify by inspecting `ats_scrape_jobs` table after a batch run
- [ ] **Workday endpoint discovery:** No Workday server suffix is hardcoded; all endpoints discovered from careers page — verify by checking ATS registry for `wd_server` field derivation
- [ ] **`__NEXT_DATA__` fallback:** Presence check runs before extraction; Firecrawl fallback triggers when absent — verify by testing against an App Router Next.js site
- [ ] **Supabase batch size:** Upsert chunked at 500 rows maximum — verify by checking upsert function for batch-size constant
- [ ] **Separate scraper modules:** ATS scraper does not import from `cron_scraper.sh` or share its process — verify by checking launchd plist files for separation
- [ ] **`ats_content_hash` column:** Schema migration adds this column before ATS scraping runs — verify with `\d jobs` in psql
- [ ] **Text normalization:** All ATS-derived text runs through `html.unescape()` + `unicodedata.normalize("NFKC")` before DB write — verify by searching DB for literal `&amp;` strings

**Carried from original milestone (all completed):**

- [x] **Scraper:** Handles `<details>/<summary>` multi-location HTML in table cells
- [x] **Scraper:** `↳` continuation rows correctly inherit company name
- [x] **Deduplication:** `normalized_key` is consistent across scrape runs
- [x] **Enrichment:** Re-enrichment only fires when content hash changes
- [x] **Embeddings:** `embedding_model` column populated on every job row
- [x] **pgvector:** HNSW index is used by query planner (EXPLAIN ANALYZE confirmed)
- [x] **Matching:** Cold-start path triggers for new users
- [x] **Sponsorship filter:** Null/unknown sponsorship jobs surface correctly

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Firecrawl credits drained by `extract` feature | MEDIUM (credits already spent) | Switch to `scrape` immediately; no technical recovery for spent credits |
| Batch job stuck "scraping" for 12h | LOW | Cancel via `cancel_batch_scrape()`; re-run with smaller batch; check Firecrawl status page |
| Statement timeout killed 33K batch | LOW | Re-run with 500-row chunk size; confirm no partial-batch enrichment triggered |
| `content_hash` modification triggered full re-enrichment | HIGH (LLM cost) | Fix hash design to use `ats_content_hash`; no recovery for API spend |
| HNSW index 100x UPDATE degradation causing timeouts | MEDIUM | Batch non-vector updates separately; consider table split for long-term fix |
| `__NEXT_DATA__` extraction returns empty after site migration | LOW | Enable Firecrawl fallback path; update source registry to flag App Router sites |
| ATS scraping stage breaks GitHub scraper | HIGH | Immediately revert ATS module; GitHub scraper must be isolated and independently deployable |
| Duplicate jobs due to bad ID function | HIGH | Deduplicate DB, re-derive IDs, re-run enrichment for merged records |

---

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Firecrawl SDK timeout unit bug | Firecrawl integration phase | asyncio-level timeout test against slow URL |
| Firecrawl 5x credit multiplier on extract | Architecture decision (pre-implementation) | `usage.credits_used = 1` per scrape call |
| Firecrawl batch jobs stuck | Firecrawl integration phase | Job ID stored in DB; 90-min max-age check in scraper logic |
| ATS platform structure divergence (Workday server suffix) | ATS scraping phase | Endpoint discovery step confirmed for ≥3 distinct employers |
| Cloudflare blocking direct Workday CXS calls | ATS scraping phase | Test enterprise employer pages; Firecrawl route for blocked domains |
| `__NEXT_DATA__` scraping fragility | ATS/job board scraping phase | Presence check + fallback path; tested against App Router site |
| Supabase 33K batch statement timeout | ATS data ingestion phase | Upsert chunked at 500 rows; no statement timeout errors in logs |
| SiliconFlow dual RPM/TPM limits | LLM enrichment integration phase | Dual-dimension rate limiter class; no 429s at target throughput |
| `content_hash` corruption from new ATS fields | Data integration phase (schema) | `ats_content_hash` column in migration; first ATS scrape does not re-enrich existing rows |
| HNSW non-vector UPDATE degradation | ATS data integration phase | Vector and non-vector updates separated in upsert logic |
| Stale/ghost ATS listings | ATS scraping phase | 404 detection marks `is_active = false`; 30-day TTL enforced |
| New scraping stage breaks existing pipeline | Architecture decision (pre-implementation) | GitHub scraper runs successfully in isolation after ATS module deployed |
| Encoding contamination from ATS HTML | ATS parsing phase | Text normalization utility; no `&amp;` literals in DB |

---

## Sources

**New milestone sources:**
- Firecrawl Rate Limits (official) — https://docs.firecrawl.dev/rate-limits
- Firecrawl Python SDK (official) — https://docs.firecrawl.dev/sdks/python
- Firecrawl GitHub issue #1848: SDK timeout unit conversion bug — https://github.com/firecrawl/firecrawl/issues/1848
- Firecrawl community: batch scrape jobs stuck in "scraping" — https://www.answeroverflow.com/m/1324115093444628591
- Firecrawl pricing analysis: 5x credit multiplier on extract — https://scrapegraphai.com/blog/firecrawl-pricing
- Lever Postings API (official) — https://github.com/lever/postings-api
- Greenhouse Job Board API (official) — https://developers.greenhouse.io/job-board.html
- Workday CXS API community discovery — https://news.ycombinator.com/item?id=39624542
- Cloudflare bot detection mechanisms (2025) — https://alterlab.io/blog/bypass-cloudflare-bot-protection-web-scraping
- pgvector GitHub issue #875: HNSW 100x degradation on non-vector column UPDATE — https://github.com/pgvector/pgvector/issues/875
- pgvector GitHub issue #810: HNSW INSERT performance — https://github.com/pgvector/pgvector/issues/810
- Supabase Docs: statement timeouts (official) — https://supabase.com/docs/guides/database/postgres/timeouts
- Supabase Docs: avoiding timeouts in long-running queries — https://supabase.com/docs/guides/troubleshooting/avoiding-timeouts-in-long-running-queries-6nmbdN
- SiliconFlow rate limits (official) — https://docs.siliconflow.cn/en/userguide/rate-limits/rate-limit-and-upgradation
- ScrapingAnt: building a web data quality layer — https://scrapingant.com/blog/building-a-web-data-quality-layer-deduping-canonicalization
- Job Board Scraping 2025 guide — https://www.jobboardly.com/blog/job-board-scraping-complete-guide-2025
- Web scraping legal compliance 2025 — https://groupbwt.com/blog/is-web-scraping-legal/
- Forage.ai: character encoding bugs in scraping pipeline — https://forage.ai/blog/character-encoding-bugs-web-scraping-guide/

**Original milestone sources (retained):**
- GitHub Changelog: rate limits for unauthenticated requests (May 8, 2025) — https://github.blog/changelog/2025-05-08-updated-rate-limits-for-unauthenticated-requests/
- pgvector GitHub: HNSW index not used for KNN queries — https://github.com/pgvector/pgvector/issues/835
- Crunchy Data: pgvector performance for developers — https://www.crunchydata.com/blog/pgvector-performance-for-developers
- AWS Blog: pgvector indexing deep dive — https://aws.amazon.com/blogs/database/optimize-generative-ai-applications-with-pgvector-indexing-a-deep-dive-into-ivfflat-and-hnsw-techniques/
- SimplifyJobs Summer2026-Internships README — https://github.com/SimplifyJobs/Summer2026-Internships/blob/dev/README.md

---
*Pitfalls research for: job scraping + matching engine — Firecrawl scraping, ATS page parsing, and search milestone*
*Original research: 2026-03-25 | Updated: 2026-03-31*
