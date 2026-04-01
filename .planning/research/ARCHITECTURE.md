# Architecture Research

**Domain:** Firecrawl integration into existing wekruit-matching pipeline
**Researched:** 2026-03-31
**Confidence:** HIGH

---

## Context: What Already Exists

Before any new components, the pipeline looks like this:

```
Stage 1: scrape_all()
  ├── SimplifyJobs GitHub READMEs (httpx, markdown parsing)
  └── JobRight GitHub repos (httpx, markdown parsing)
         ↓ upsert_jobs() → jobs table

Stage 2a: enrich_from_jobright.enrich_all_jobs()
  └── JobRight.ai page fetch (httpx, __NEXT_DATA__ JSON parse)
      → fills job_description, required_skills, salary_range,
        seniority_level, benefits, qualifications, sponsorship
         ↓ UPDATE jobs WHERE primary_url LIKE 'https://jobright.ai/%'

Stage 2b: enrichment.worker.enrich_pending()
  └── SiliconFlow Qwen3-8B (OpenAI-compatible API)
      → fills industry, company_size, required_skills, sponsorship
         ↓ UPDATE jobs WHERE enriched_at IS NULL

Stage 3: embedding.worker.embed_all()
  └── OpenAI text-embedding-3-small
      → fills embedding vector(1536), embedding_model
         ↓ UPDATE jobs WHERE embedded_at IS NULL AND enriched_at IS NOT NULL
```

**Key observation:** Stage 2a (`enrich_from_jobright`) is already the model for what Firecrawl integration should look like — it fills the job detail columns that the initial scrape leaves empty. Firecrawl becomes a parallel path alongside `enrich_from_jobright`, not a replacement for it.

---

## Standard Architecture

### System Overview: Post-Firecrawl Pipeline

```
┌─────────────────────────────────────────────────────────────────────┐
│                         INGESTION LAYER                             │
│                                                                     │
│  ┌────────────────┐    ┌──────────────────┐                         │
│  │ SimplifyJobs + │    │  Firecrawl Search │                         │
│  │ JobRight GitHub│    │  (new employer    │                         │
│  │ READMEs        │    │   career URLs)    │                         │
│  └───────┬────────┘    └────────┬──────────┘                         │
│          │                     │                                     │
│          ▼                     ▼                                     │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │              Stage 1: scrape_all() — unchanged                 │   │
│  │   SimplifyJobs + JobRight GitHub → upsert_jobs()              │   │
│  └───────────────────────────────┬───────────────────────────────┘   │
└──────────────────────────────────┼──────────────────────────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      JD ENRICHMENT LAYER                            │
│                                                                     │
│  ┌──────────────────────┐   ┌──────────────────────────────────┐    │
│  │  Stage 2a (existing) │   │  Stage 2b: Firecrawl Enricher    │    │
│  │  enrich_from_jobright│   │  (NEW — for non-JobRight URLs)   │    │
│  │                      │   │                                  │    │
│  │  target: jobs WHERE  │   │  target: jobs WHERE              │    │
│  │  primary_url LIKE    │   │  primary_url NOT LIKE            │    │
│  │  'jobright.ai/%'     │   │  'jobright.ai/%'                 │    │
│  │  AND job_description │   │  AND job_description IS NULL     │    │
│  │  IS NULL             │   │  AND primary_url IS NOT NULL     │    │
│  │                      │   │                                  │    │
│  │  method: httpx GET   │   │  routing:                        │    │
│  │  + __NEXT_DATA__     │   │  1. ATS JSON APIs (free,fast)    │    │
│  │  JSON parse ($0 cost)│   │     → Greenhouse, Lever          │    │
│  │                      │   │  2. Firecrawl /scrape (JS-render)│    │
│  │                      │   │     → Workday, Ashby, iCIMS etc  │    │
│  │                      │   │  3. Firecrawl /extract (LLM)     │    │
│  │                      │   │     → unstructured employer pages│    │
│  └──────────┬───────────┘   └──────────────┬───────────────────┘    │
│             │                              │                         │
│             └──────────┬───────────────────┘                         │
│                        │                                             │
│                        ▼                                             │
│  ┌──────────────────────────────────────────────────────────────┐    │
│  │    Stage 2c (existing): LLM metadata classifier              │    │
│  │    SiliconFlow Qwen3-8B → industry, company_size,            │    │
│  │    required_skills (where 2a/2b didn't fill them)            │    │
│  └──────────────────────────────────────────────────────────────┘    │
└─────────────────────────────┬───────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      EMBEDDING LAYER (unchanged)                    │
│   Stage 3: embed_all() — OpenAI text-embedding-3-small             │
│   Input text now richer: title + company + skills + jd_text        │
└─────────────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Status |
|-----------|----------------|--------|
| `scraper/run.py` | Fetch job listings from GitHub READMEs, upsert to DB | Existing, unchanged |
| `scraper/enrich_from_jobright.py` | Fetch JobRight.ai detail pages via httpx + `__NEXT_DATA__` parse, fill JD columns | Existing, unchanged |
| `scraper/firecrawl_enricher.py` | NEW — fetch non-JobRight job pages via Firecrawl (3-tier routing), fill same JD columns | New |
| `scraper/ats_enricher.py` | NEW — free ATS JSON API fetchers for Greenhouse/Lever (no Firecrawl credits needed) | New |
| `scraper/url_classifier.py` | NEW — classify a primary_url into routing tier: jobright / ats-json / firecrawl-scrape / firecrawl-extract / unknown | New |
| `pipeline/daily.py` | Orchestrate all stages in sequence | Existing, modified to insert new stages |
| `enrichment/worker.py` | LLM metadata classification for jobs without enriched_at | Existing, unchanged |
| `embedding/worker.py` | Generate embeddings for enriched jobs | Existing, unchanged |

---

## Recommended Project Structure Changes

The existing `src/wekruit_matching/` structure needs these additions:

```
src/wekruit_matching/
├── scraper/
│   ├── enrich_from_jobright.py    # existing — unchanged
│   ├── url_classifier.py          # NEW: classify URL → routing tier
│   ├── ats_enricher.py            # NEW: Greenhouse/Lever JSON API fetchers
│   ├── firecrawl_enricher.py      # NEW: Firecrawl /scrape + /extract + search
│   └── run_jd_enrichment.py       # NEW: orchestrator that routes jobs to the right fetcher
│
├── pipeline/
│   └── daily.py                   # MODIFIED: insert new JD enrichment stage
│
└── config.py                      # MODIFIED: add FIRECRAWL_API_KEY setting
```

**Why this structure:**

- `url_classifier.py` stays separate because the routing logic will evolve independently of fetch logic. URL pattern matching (does URL match greenhouse.io? lever.co?) is distinct from the HTTP calls.
- `ats_enricher.py` is split from `firecrawl_enricher.py` because ATS JSON APIs are free, synchronous, and reliable — they should not consume Firecrawl credits. Mixing them would complicate credit budgeting.
- `run_jd_enrichment.py` is a new orchestrator that replaces calling `enrich_from_jobright` alone. It dispatches to the right fetcher per job and can be called from `daily.py` as a single entry point.

---

## Architectural Patterns

### Pattern 1: Tiered Fetcher Routing

**What:** Before fetching a job page, classify the URL to choose the cheapest method that can succeed. Apply methods in ascending cost order, short-circuiting when one succeeds.

**When to use:** Every time a job record with `job_description IS NULL` and a non-null `primary_url` is processed.

**Trade-offs:** Adds one classification step per job. Saves significant cost — ATS JSON APIs are free, Firecrawl /scrape costs 1 credit, /extract with LLM costs up to 5 credits.

**Routing table (ascending cost):**

| Tier | Trigger | Method | Cost |
|------|---------|--------|------|
| 0 | `jobright.ai/` in URL | `enrich_from_jobright` (existing) | $0 |
| 1 | `greenhouse.io/` or `lever.co/` in URL | `ats_enricher` — direct JSON API | $0 |
| 2 | Known JS-heavy ATS (Workday, Ashby, iCIMS, Rippling, etc.) | Firecrawl `/scrape` → markdown parse | 1 credit |
| 3 | Unknown employer career page with detectable structure | Firecrawl `/scrape` → markdown parse | 1 credit |
| 4 | Unstructured or employer-specific pages that resist markdown parsing | Firecrawl `/extract` with JD schema | 5 credits |

**Example routing:**

```python
# url_classifier.py
def classify_url(url: str) -> str:
    """Return routing tier for a job application URL."""
    if not url:
        return "unknown"
    lower = url.lower()
    if "jobright.ai" in lower:
        return "jobright"
    if "greenhouse.io" in lower or "lever.co" in lower or "ashbyhq.com" in lower:
        return "ats-json"
    if any(p in lower for p in ["myworkday.com", "wd1.myworkdayjobs", "wd3.myworkdayjobs",
                                  "icims.com", "rippling.com", "jobvite.com", "workable.com"]):
        return "firecrawl-scrape"
    # Default: try scrape first, fall back to extract if it returns no JD text
    return "firecrawl-scrape"
```

### Pattern 2: Credit-Aware Batch Processing with DB Tracking

**What:** Track which jobs have been attempted by Firecrawl (success or failure) in a DB column so re-runs don't re-spend credits on already-attempted jobs.

**When to use:** Any Firecrawl call that costs credits.

**Trade-offs:** Requires a new DB column (`jd_fetch_attempted_at`, `jd_fetch_source`). Prevents duplicate spend — critical at scale.

**DB columns needed (new migration):**

```sql
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS jd_fetch_source TEXT;
-- Values: 'jobright' | 'greenhouse_api' | 'lever_api' | 'firecrawl_scrape'
--         | 'firecrawl_extract' | 'search' | 'failed' | NULL (not attempted)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS jd_fetch_attempted_at TIMESTAMPTZ;
```

**Query for jobs to process:**

```sql
SELECT job_id, primary_url, role_title, company_name
FROM jobs
WHERE status = 'active'
  AND job_description IS NULL
  AND primary_url IS NOT NULL
  AND primary_url NOT LIKE 'https://jobright.ai/%'
  AND jd_fetch_attempted_at IS NULL  -- never attempted
ORDER BY first_seen_at DESC
LIMIT 100
```

### Pattern 3: Fallback Chain Within Firecrawl Tier

**What:** For jobs routed to Firecrawl, try `/scrape` first. If the returned markdown lacks a detectable job description (under 200 chars, or no keywords like "responsibilities", "requirements", "qualifications"), escalate to `/extract` with a JD schema.

**When to use:** Jobs where URL tier is `firecrawl-scrape` or `firecrawl-extract`.

**Trade-offs:** Escalation from scrape (1 credit) to extract (5 credits) costs more but only triggers when the cheaper method fails. In practice, most mainstream ATS pages return good markdown.

**Example:**

```python
def fetch_with_firecrawl(url: str, fc: Firecrawl) -> dict | None:
    """Try /scrape first, escalate to /extract if JD text is insufficient."""
    # Attempt 1: scrape (1 credit)
    result = fc.scrape(url, formats=["markdown"], timeout=30000)
    markdown = result.get("markdown", "") or ""

    if _has_jd_content(markdown):
        return _parse_markdown_to_jd(markdown)

    # Attempt 2: extract with schema (5 credits)
    schema = {
        "type": "object",
        "properties": {
            "job_description": {"type": "string"},
            "responsibilities": {"type": "array", "items": {"type": "string"}},
            "qualifications": {"type": "array", "items": {"type": "string"}},
            "salary_range": {"type": "string"},
            "skills": {"type": "array", "items": {"type": "string"}},
        }
    }
    extracted = fc.extract(urls=[url], schema=schema)
    if extracted and extracted.get("job_description"):
        return extracted
    return None


def _has_jd_content(markdown: str) -> bool:
    """Heuristic: markdown likely contains a real JD if it's long enough
    and mentions at least one job-description keyword."""
    if len(markdown) < 200:
        return False
    keywords = {"responsibilities", "requirements", "qualifications",
                "experience", "skills", "duties", "about the role", "what you'll do"}
    text_lower = markdown.lower()
    return any(kw in text_lower for kw in keywords)
```

### Pattern 4: Firecrawl Search for Direct Employer URL Discovery

**What:** For jobs where `primary_url` points to a job aggregator (not an employer page), use Firecrawl `/search` to find the employer's direct careers page URL, then scrape that.

**When to use:** When `primary_url` is a redirect tracker, LinkedIn, or an aggregator that blocks scraping. Also useful for jobs that have no `primary_url` at all — search for `"{company} {role} careers site:company.com"`.

**Trade-offs:** Search costs 2 credits per 10 results. Only worth doing for high-value jobs (rare role titles, target companies). Not worth doing for all 76K jobs — apply selectively.

**Example:**

```python
def discover_employer_url(company: str, role: str, fc: Firecrawl) -> str | None:
    """Use Firecrawl search to find a direct employer career page URL."""
    query = f"{company} {role} job application site:{company.lower().replace(' ', '')}.com"
    results = fc.search(query, limit=5)
    for r in (results or []):
        url = r.get("url", "")
        # Prefer direct employer domains, not aggregators
        if not any(agg in url for agg in ["linkedin.com", "indeed.com",
                                           "glassdoor.com", "simplyhired.com"]):
            return url
    return None
```

### Pattern 5: ATS JSON API Fetchers (Greenhouse + Lever, Free)

**What:** Greenhouse and Lever both expose unauthenticated public JSON APIs. These return full JD text, skills-adjacent requirements, and location without any scraping or credits.

**When to use:** Any job where `primary_url` contains `greenhouse.io` or `lever.co`.

**ATS API shapes:**

**Greenhouse** — `https://boards-api.greenhouse.io/v1/boards/{company}/jobs/{job_id}`
Returns: `title`, `location.name`, `content` (HTML JD), `departments[]`, `offices[]`, `absolute_url`

**Lever** — `https://api.lever.co/v0/postings/{company}/{job_id}`
Returns: `text` (title), `description` (HTML), `lists[]` (requirements sections), `categories.location`, `categories.team`

**Implementation:** Parse HTML from `content`/`description` field with a simple regex strip (same `_clean_html()` already in `enrich_from_jobright.py`). Extract company slug and job ID from URL with regex.

---

## Data Flow

### New JD Enrichment Stage in Daily Pipeline

```
[daily.py Stage 2a — existing, unchanged]
  enrich_from_jobright() targets: jobright.ai URLs
         ↓
[daily.py Stage 2b — NEW]
  run_jd_enrichment() targets: all other URLs
  For each job WHERE job_description IS NULL
    AND primary_url IS NOT NULL
    AND primary_url NOT LIKE 'jobright.ai/%'
    AND jd_fetch_attempted_at IS NULL:

    url_classifier.classify_url(primary_url)
         ↓ tier = "ats-json"        → ats_enricher.fetch_greenhouse() or fetch_lever()
         ↓ tier = "firecrawl-scrape" → firecrawl_enricher.scrape_url()
                                          ↓ if _has_jd_content(): parse markdown
                                          ↓ else: escalate to extract()
         ↓ tier = "search"          → firecrawl_enricher.discover_and_scrape()
         ↓ tier = "unknown"         → skip (mark attempted, log)

  Write result to DB:
    UPDATE jobs SET
      job_description = ...,
      core_responsibilities = ...,
      qualifications = ...,
      salary_range = ...,
      jd_fetch_source = '...',
      jd_fetch_attempted_at = NOW()
    WHERE job_id = ...

[daily.py Stage 2c — existing: LLM metadata enrichment]
  Now has richer job_description text to work with for classification
         ↓
[daily.py Stage 3 — existing: embeddings]
  Embedding text = title + company + skills + job_description[:500]
  (richer input → better semantic matching)
```

### Credit Budget Planning

Assuming 76K jobs total, 70% already enriched, 30% needing JD fetch:
- ~23K jobs need JD enrichment
- Estimated URL distribution:
  - ~40% jobright.ai → already handled by Stage 2a ($0)
  - ~15% Greenhouse/Lever → ATS JSON API ($0)
  - ~30% known JS ATS (Workday, etc.) → Firecrawl /scrape (1 credit each = ~5K credits)
  - ~10% unknown employer pages → Firecrawl /scrape with fallback (~3K credits)
  - ~5% completely opaque → Firecrawl /extract (~1.2K credits, 5 each)

**Total estimated: ~9K credits for initial backfill.**

At $16/month for 3K credits or self-hosted for $0, the right choice is to self-host Firecrawl for the initial bulk run (AGPL Docker image, runs on the existing server), then potentially switch to the managed API for ongoing daily incremental work where volume is low (50-200 new jobs/day).

---

## DB Schema Changes Needed

One new alembic migration (`0004_add_jd_fetch_tracking.py`):

```sql
-- Track which method filled the JD, and whether we've attempted it
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS jd_fetch_source TEXT;
-- Allowed values: 'jobright' | 'greenhouse_api' | 'lever_api'
--   | 'firecrawl_scrape' | 'firecrawl_extract' | 'search'
--   | 'failed' (tried, got nothing) | NULL (not yet attempted for non-jobright)

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS jd_fetch_attempted_at TIMESTAMPTZ;
-- NULL = never attempted  |  timestamp = last attempt time
-- Used to skip re-processing on subsequent cron runs

-- Index for the enrichment queue query
CREATE INDEX IF NOT EXISTS ix_jobs_jd_pending
  ON jobs (status, jd_fetch_attempted_at)
  WHERE job_description IS NULL
    AND primary_url IS NOT NULL;
```

**Why these columns:**
- `jd_fetch_source` answers "why does this job have/lack a JD?" and enables analytics (what % of Workday pages succeeded?). Also prevents duplicate Firecrawl credit spend on retry runs.
- `jd_fetch_attempted_at` is the gate for the incremental queue — `WHERE jd_fetch_attempted_at IS NULL` ensures we only process each job once unless explicitly reset.
- The partial index on `(status, jd_fetch_attempted_at) WHERE job_description IS NULL AND primary_url IS NOT NULL` directly accelerates the enrichment queue query.

**Note:** `job_description`, `core_responsibilities`, `qualifications`, `salary_range`, `benefits` columns already exist from migration 0003. No changes needed to those.

---

## Integration Points with Existing Code

### `enrich_from_jobright.py` — Preserved Unchanged

This is the highest-value enrichment path (free, structured, reliable). The new Firecrawl enricher is additive — it handles URLs that `enrich_from_jobright` explicitly skips (non-jobright.ai URLs). No changes to `enrich_from_jobright.py` are needed.

**Existing query in `enrich_from_jobright.enrich_all_jobs()`:**
```sql
WHERE status = 'active'
  AND primary_url LIKE 'https://jobright.ai/%'
  AND (required_skills IS NULL OR required_skills = '{}')
```

**New Firecrawl enricher query:**
```sql
WHERE status = 'active'
  AND (job_description IS NULL OR job_description = '')
  AND primary_url IS NOT NULL
  AND primary_url NOT LIKE 'https://jobright.ai/%'
  AND jd_fetch_attempted_at IS NULL
```

These two queries are mutually exclusive — no job can match both.

### `pipeline/daily.py` — Minimal Change

Insert one new stage call between Stage 2a and Stage 2b:

```python
# --- Stage 2a: JobRight Page Enrichment (existing, FREE) ---
logger.info("=== Stage 2a: JobRight Page Enrichment (free) ===")
with get_connection() as conn:
    jobright_stats = enrich_jobright(conn, max_workers=8, batch_size=50)

# --- Stage 2b: Firecrawl JD Enrichment (NEW) ---
logger.info("=== Stage 2b: Firecrawl JD Enrichment ===")
from wekruit_matching.scraper.run_jd_enrichment import run_jd_enrichment
firecrawl_stats = run_jd_enrichment()

# --- Stage 2c: LLM metadata classifier (existing) ---
logger.info("=== Stage 2c: LLM Enrichment (metadata classification) ===")
enrich_stats = enrich_all()
```

### `config.py` — One New Field

```python
# Firecrawl (optional — pipeline degrades gracefully if absent)
firecrawl_api_key: str = Field("", repr=False)
firecrawl_base_url: str = Field("https://api.firecrawl.dev")
# Set firecrawl_base_url to http://localhost:3002 for self-hosted
```

Using `Field("")` (empty default) means the enricher can check `if not settings.firecrawl_api_key: skip` and the pipeline continues without Firecrawl configured. Graceful degradation is important — the pipeline was working before, it shouldn't break without a Firecrawl key.

### `enrichment/worker.py` — No Change Needed

The LLM classifier reads `enriched_at IS NULL`. When Stage 2b fills `job_description`, the classifier will use that text to make better `industry`, `company_size`, and `required_skills` decisions. But the worker itself doesn't need modification — it already uses `role_title`, `company_name`, `location_raw` for classification. If we want to pass `job_description` into the LLM prompt, that's an optional enhancement to `enrichment/classifier.py`'s prompt, not a structural change.

---

## Build Order

Dependencies are strict. Build in this exact order:

```
Step 1: DB migration (0004_add_jd_fetch_tracking.py)
  └── All new enrichment queries depend on jd_fetch_attempted_at column

Step 2: url_classifier.py
  └── No external dependencies. Pure string matching.
  └── Unit-testable with no DB or network.

Step 3: ats_enricher.py (Greenhouse + Lever JSON APIs)
  └── Depends on: url_classifier (to know which ATS to call)
  └── Free to run, validates approach before spending Firecrawl credits
  └── Test against real Greenhouse/Lever URLs before moving on

Step 4: firecrawl_enricher.py (scrape + extract)
  └── Depends on: url_classifier, firecrawl-py SDK
  └── Build scrape path first, validate markdown quality
  └── Add extract fallback only after scrape path is validated

Step 5: run_jd_enrichment.py (orchestrator)
  └── Depends on: url_classifier + ats_enricher + firecrawl_enricher
  └── Routes each job to the right fetcher, writes results to DB
  └── Contains the throttling, error isolation, and batch commit logic

Step 6: daily.py modification
  └── Insert run_jd_enrichment() between Stage 2a and Stage 2b
  └── Final integration point — test with --dry-run first

Step 7: embedding text enrichment (optional)
  └── Modify embedding/worker.py to include job_description[:500]
      in the text passed to text-embedding-3-small
  └── Improves match quality — do after rest of pipeline is stable
```

**Rationale for this order:**
- Steps 1-2 have no risk (schema migration + pure string matching)
- Step 3 validates the ATS enrichment concept for free before any Firecrawl spend
- Step 4 is isolated — can be tested against a handful of URLs without touching the DB
- Step 5 is where all the pieces come together, so building it last ensures its dependencies are stable
- Step 6 should be last to avoid disrupting the existing working daily pipeline during development

---

## Scaling Considerations

| Scale | Architecture Adjustments |
|-------|--------------------------|
| Daily incremental (50-200 new jobs/day) | Cloud Firecrawl API is fine at this volume; 200 credits/day stays within $16/month plan |
| Initial backfill (23K unenriched jobs) | Self-host Firecrawl via Docker Compose on the existing server; no credit cost; takes 2-4 hours at 8 concurrent workers |
| Rate limiting from target sites | Implement per-domain rate limits in `run_jd_enrichment.py` — different sites have different tolerances; jobright pattern (0.3s sleep) is a good model |

### Scaling Priorities

1. **First bottleneck: Firecrawl credit budget.** ATS JSON path (Greenhouse/Lever) must be exhausted first — it's free and covers ~15% of URLs. Self-hosting for bulk runs eliminates credit cost entirely during backfill.
2. **Second bottleneck: Per-domain rate limiting.** Without per-domain throttling, aggressive parallel scraping will get the IP blocked by Workday, iCIMS etc. Apply a `collections.defaultdict(deque)` tracking last-access-time per domain.

---

## Anti-Patterns

### Anti-Pattern 1: Using Firecrawl for All URLs

**What people do:** Route every job URL through Firecrawl regardless of whether a cheaper method exists.
**Why it's wrong:** Greenhouse and Lever have free public JSON APIs that return the full JD. Using Firecrawl on those URLs wastes 1-5 credits per job when $0 alternatives exist.
**Do this instead:** `url_classifier.py` routes Greenhouse/Lever to `ats_enricher.py` (free), only unknown/JS-heavy pages go to Firecrawl.

### Anti-Pattern 2: No Attempt Tracking Column

**What people do:** Re-query `WHERE job_description IS NULL` on every cron run without tracking whether Firecrawl was already tried.
**Why it's wrong:** A failed fetch (404, JS wall, empty markdown) will re-spend credits on every daily run indefinitely. At 23K unenriched jobs × $0.01/credit, that's real money.
**Do this instead:** Set `jd_fetch_attempted_at = NOW()` and `jd_fetch_source = 'failed'` on every attempt, successful or not. The queue query filters `WHERE jd_fetch_attempted_at IS NULL`.

### Anti-Pattern 3: Mixing Firecrawl State with JobRight State

**What people do:** Reuse `enriched_at` to gate Firecrawl enrichment, conflating two different enrichment stages.
**Why it's wrong:** `enriched_at` is set by the LLM metadata classifier (Stage 2c). If a job has `enriched_at` set but no `job_description`, it means Stage 2c ran before Stage 2b could fill the JD. Using `enriched_at` as the gate for Firecrawl would skip those jobs permanently.
**Do this instead:** Use the dedicated `jd_fetch_attempted_at` column for JD fetch tracking. The two enrichment stages (JD text fetch and LLM metadata classification) gate on different columns for exactly this reason.

### Anti-Pattern 4: Calling Firecrawl /extract on Every Page

**What people do:** Always use `/extract` (LLM-backed, 5 credits) instead of trying `/scrape` (1 credit) first.
**Why it's wrong:** Most ATS pages have consistent enough structure that plain markdown is sufficient. `/extract` is 5× more expensive and slower.
**Do this instead:** Use `_has_jd_content(markdown)` heuristic after `/scrape`. Only escalate to `/extract` when the markdown is short or lacks JD keywords. In practice, ~80% of pages should resolve at the scrape tier.

### Anti-Pattern 5: Self-Hosting Firecrawl for Daily Incremental Runs

**What people do:** Run self-hosted Firecrawl (Docker Compose on the matching server) as the permanent solution for all scraping.
**Why it's wrong:** Self-hosting requires Redis + Postgres + queue workers running 24/7. At 50-200 new jobs/day, the operational overhead exceeds the $2-3/month API cost. Self-hosting is the right choice for the initial 23K-job backfill, not for steady-state operations.
**Do this instead:** Self-host for the bulk backfill (no credit cost), then switch to the managed Firecrawl API for daily incremental work. The `firecrawl_base_url` config field makes this a one-line change.

---

## External Services

| Service | Integration Pattern | Notes |
|---------|---------------------|-------|
| Firecrawl API (cloud) | `firecrawl-py` SDK, `Firecrawl(api_key=...)` | `firecrawl-py` v4.21.0. Set `base_url` to override for self-hosted |
| Firecrawl (self-hosted) | Same SDK, `base_url="http://localhost:3002"` | Docker Compose: needs Redis + Postgres; AGPL license; 100% feature parity for scrape/extract |
| Greenhouse Job Board API | `httpx.get("https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}")` | No auth. Returns JSON with `content` field (HTML JD). Public API, documented. |
| Lever Jobs API | `httpx.get("https://api.lever.co/v0/postings/{slug}/{id}")` | No auth. Returns JSON with `description` (HTML). Public, documented. |
| Ashby HQ API | `httpx.get("https://api.ashbyhq.com/posting-api/job-board/{slug}")` | No auth. Returns JSON job board. Ashby is growing ATS in tech startups. |

---

## Sources

- [Firecrawl Python SDK — PyPI firecrawl-py v4.21.0](https://pypi.org/project/firecrawl-py/) — HIGH confidence, verified March 25 2026
- [Firecrawl /extract endpoint documentation](https://docs.firecrawl.dev/features/extract) — HIGH confidence
- [Firecrawl /search endpoint documentation](https://docs.firecrawl.dev/features/search) — HIGH confidence
- [Firecrawl /scrape endpoint documentation](https://docs.firecrawl.dev/features/scrape) — HIGH confidence
- [Firecrawl self-hosting guide](https://docs.firecrawl.dev/contributing/self-host) — HIGH confidence; confirmed no-credit-limit for self-hosted
- [Greenhouse Job Board API documentation](https://developers.greenhouse.io/job-board.html) — HIGH confidence; confirmed public, no auth required
- [Lever public jobs API — confirmed via community sources](https://github.com/MarcusKyung/greenhouse.io-scraper) — MEDIUM confidence; API endpoint structure verified
- [Firecrawl pricing: 1 credit/scrape, 5 credits/extract, 2 credits per 10 search results](https://www.firecrawl.dev/glossary/web-scraping-apis/how-web-scraping-apis-handle-rate-limiting-quotas) — MEDIUM confidence (pricing pages change frequently)
- [Firecrawl caching: max_age parameter, storeInCache option](https://www.firecrawl.dev/changelog) — MEDIUM confidence
- Existing codebase analysis: `enrich_from_jobright.py`, `pipeline/daily.py`, `db/tables.py`, `config.py` — HIGH confidence (direct code reading)

---

*Architecture research for: Firecrawl integration into wekruit-matching pipeline*
*Researched: 2026-03-31*
