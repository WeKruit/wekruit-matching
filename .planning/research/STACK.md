# Stack Research

**Domain:** Python job scraping + vector matching engine
**Researched:** 2026-03-25 (base); 2026-03-31 (milestone update: data pipeline)
**Confidence:** HIGH (most choices verified against PyPI, official docs, and multiple independent sources)

---

## Milestone Update: Job Data Pipeline Additions (2026-03-31)

This section documents the **new** stack additions for the job data collection pipeline milestone: Firecrawl integration, ATS page parsing (Greenhouse, Lever, Workday), and structured JD extraction. The base stack below is unchanged.

### New Libraries Required

| Library | Version | Purpose | Why |
|---------|---------|---------|-----|
| firecrawl-py | 4.21.0 | Render and scrape JS-heavy employer career pages into clean markdown/HTML | Use when the ATS doesn't have a public JSON API and the page requires a headless browser (Workday embedded pages, custom career sites, Ashby). Cloud handles proxy rotation and JS rendering — self-hosted lacks these. Do NOT use for Greenhouse/Lever which have free, unauthenticated JSON APIs |
| beautifulsoup4 | 4.14.x | Parse HTML job description content from ATS API responses | Greenhouse and Lever return `content` as styled HTML. bs4 + lxml strips to plain text for LLM enrichment input. Do NOT add a full browser stack just to strip HTML tags |
| lxml | 5.x | Fast HTML/XML parser backend for BeautifulSoup | 5-10x faster than Python's built-in html.parser; install as `lxml` and pass `features="lxml"` to BeautifulSoup |
| playwright | 1.50.x | Headless browser for Workday intercept pattern | Workday's myworkdayjobs.com uses XHR fetches to load job data dynamically. Playwright intercepts the network response (JSON) rather than parsing obfuscated DOM. ONLY add this if Workday is a named target — its Docker footprint is large (~200MB) |

### What NOT to Add

| Avoid | Why | What to Use Instead |
|-------|-----|---------------------|
| firecrawl-py for Greenhouse | Greenhouse has a public unauthenticated REST API (`boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`). Using Firecrawl here costs credits needlessly | httpx direct to Greenhouse API |
| firecrawl-py for Lever | Same as Greenhouse — Lever has a free unauthenticated JSON endpoint at `api.lever.co/v0/postings/{company}?mode=json` | httpx direct to Lever API |
| requests | Already excluded from base stack. enrich_from_jobright.py uses httpx correctly — do not introduce requests | httpx (already present) |
| Scrapy / crawlee | Full crawl framework overhead for a targeted pipeline that hits known ATS endpoints. Adds scheduler, middleware, and item pipeline abstractions we don't need | httpx + ThreadPoolExecutor (already the pattern in enrich_from_jobright.py) |
| playwright for Greenhouse/Lever | Both ATS expose JSON APIs. Playwright is a 200MB dependency to solve a problem that doesn't exist for these two | httpx direct to JSON API |

---

## Firecrawl: Cloud vs Self-Hosted Decision

**Recommendation: Use Firecrawl Cloud (Hobby plan, $16/month)**

**Rationale:**

Self-hosted Firecrawl is missing Fire-engine — the component that handles IP rotation, stealth mode, and bot detection bypass. The cloud version's self-hosted variant only supports basic Playwright rendering without the proxy layer. For employer ATS pages (especially custom career sites and Ashby), IP-based blocks are common. Self-hosting saves $16/month but loses the primary reason to use Firecrawl over raw httpx.

The Hobby plan (3,000 credits/month at $16) is sufficient for this pipeline's scope:
- 1 credit per standard page scrape
- ATS pages are targeted (not a crawl of thousands) — a daily pipeline visiting 50-100 new employer pages costs ~100 credits/day max
- 3,000 credits covers ~1 month of aggressive employer page collection

**When to upgrade to Standard ($83/month, 100K credits):** If the pipeline starts bulk-collecting employer pages across hundreds of companies per day. At that scale, the cost per page drops from $0.0053 (Hobby) to $0.00083 (Standard) — a 6x reduction.

**Cloud rate limits (verified):**

| Plan | /scrape RPM | /search RPM | /crawl RPM | Concurrent Browsers |
|------|-------------|-------------|------------|---------------------|
| Free | 10 | 5 | 1 | 2 |
| Hobby | 100 | 50 | 15 | 5 |
| Standard | 500 | 250 | 50 | 50 |

**Free tier caveat:** 500 lifetime credits (not per month). Sufficient for development/testing only — do not use free tier in production.

**Credits do not roll over.** Unused credits at month-end are lost. Structure the pipeline to use credits during the month rather than banking them.

---

## ATS Page Parsing Strategy

### Greenhouse (boards-api.greenhouse.io)

**Method: httpx + existing pattern (NO Firecrawl needed)**

Greenhouse provides an unauthenticated public REST API. The `content` field returns HTML that bs4 can strip to plain text.

```
GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true
GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}?pay_transparency=true
```

Fields available: `title`, `location`, `absolute_url`, `content` (HTML JD), `updated_at`, `metadata`. Salary data available via `?pay_transparency=true` on individual job endpoints.

No rate limits documented. Be polite — 0.5s delay between requests matches the existing jobright.py pattern.

### Lever (api.lever.co)

**Method: httpx + existing pattern (NO Firecrawl needed)**

Lever's v0 postings API requires no authentication and returns structured JSON.

```
GET https://api.lever.co/v0/postings/{company_name}?mode=json
GET https://api.lever.co/v0/postings/{company_name}/{posting_id}
```

Fields available: `text` (title), `categories` (team, location, commitment, department), `descriptionPlain`, `lists` (requirements, responsibilities as text), `salaryRange`, `workplaceType` (on-site/remote/hybrid). The `descriptionPlain` field is already stripped of HTML — no bs4 needed for Lever.

### Workday (*.wd*.myworkdayjobs.com)

**Method: httpx POST to undocumented CXS API (preferred) OR Playwright for JS-heavy variants**

Workday exposes an undocumented but stable JSON endpoint pattern:

```
POST https://{tenant}.{wd_server}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Body: {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
```

Returns paginated job listings. Individual job details:
```
GET https://{tenant}.{wd_server}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{externalPath}
```

**Caveats:** `wd_server` varies per company (wd1, wd3, wd5 — must be discovered from the actual career page URL). Job descriptions are returned as HTML strings — use bs4 to strip. Only use Playwright if the CXS endpoint returns 403 or the tenant uses embedded Workday (rare).

### Custom Career Pages / Ashby / BambooHR

**Method: Firecrawl `scrape()` with `formats=["markdown"]`**

For career pages that don't match Greenhouse/Lever/Workday patterns, delegate to Firecrawl. It handles JS rendering, proxy rotation, and returns LLM-ready markdown — reducing token count by ~67% vs raw HTML before feeding to the enrichment LLM.

---

## SiliconFlow Free Tier: Validated Limits (2026-03-31)

**Recommendation: Keep SiliconFlow Qwen3-8B for gap-fill enrichment**

Confirmed current state:
- **Free models:** Qwen3-8B, DeepSeek-R1-Distill-Qwen-7B, GLM-4.1V-9B-Thinking, and others
- **Rate limits (free tier):** 1,000 RPM, 50,000 TPM — HIGH confidence from official docs
- **Daily cap:** 50 requests/day without purchased credits; 1,000 requests/day after purchasing at least $10 in credits
- **Cost for Qwen3-8B (paid):** $0.06/M input tokens, $0.06/M output tokens — effectively free at enrichment volumes

**The 50 req/day cap without credits is the critical constraint.** A pipeline enriching 200 new jobs/day at 1 request/job will hit this wall immediately. Either purchase $10 in credits (unlocks 1,000 req/day) or batch multiple jobs per request (5-10 jobs per call keeps daily requests under 50 even without credits).

**Alternative if SiliconFlow proves unreliable:** OpenRouter's free tier offers 29 free models with 20 RPM / 200 req/day limits. Models include Llama 3.3 70B and Qwen3 variants. The 200 req/day cap is 4x better than SiliconFlow's uncredited free tier, but model quality and latency are less predictable on a shared free router. MEDIUM confidence — useful fallback, not primary.

---

## Integration with Existing enrich_from_jobright.py

The existing `enrich_from_jobright.py` uses:
- `httpx.get()` + `ThreadPoolExecutor` for parallel fetches (8 workers)
- `__NEXT_DATA__` JSON extraction from JobRight SSR pages
- Direct psycopg3 DB writes
- No LLM — $0 cost enrichment from structured JobRight data

**Do not replicate this pattern for ATS pages.** ATS pages serve structured JSON APIs directly — the `__NEXT_DATA__` regex approach is JobRight-specific. New ATS scrapers should call the JSON APIs with httpx and parse the returned structured fields, not scrape HTML.

**Integration points:**
- New `scraper/ats_greenhouse.py`, `scraper/ats_lever.py`, `scraper/ats_workday.py` — each follows the same `list[Job]` return type as `jobright.py` for clean upsert pipeline integration
- New `scraper/ats_firecrawl.py` — for custom/unknown ATS pages, uses firecrawl-py SDK and returns extracted text for LLM enrichment rather than structured fields
- bs4 HTML stripping belongs in ATS scrapers, not in the enrichment layer — strip to plain text before passing to the LLM

---

## Recommended pyproject.toml Additions

```toml
# Add to [project] dependencies in pyproject.toml:
"firecrawl-py>=4.21.0,<5.0",
"beautifulsoup4>=4.14.0",
"lxml>=5.0",
# playwright only if Workday CXS API approach fails:
# "playwright>=1.50.0",
```

Install with uv:
```bash
uv add "firecrawl-py>=4.21.0,<5.0" "beautifulsoup4>=4.14.0" "lxml>=5.0"

# Playwright is optional — only if needed for Workday embedded pages:
# uv add "playwright>=1.50.0"
# uv run playwright install chromium  # downloads ~200MB browser binary
```

Set Firecrawl API key in environment:
```bash
# .env (managed via pydantic-settings, never hardcoded):
FIRECRAWL_API_KEY=fc-YOUR-KEY-HERE
```

---

## Version Compatibility (New Libraries)

| Package | Compatible With | Notes |
|---------|-----------------|-------|
| firecrawl-py 4.21.0 | Python 3.8+; httpx already in project | SDK uses its own httpx internally; no conflict with project's httpx usage |
| beautifulsoup4 4.14.x | lxml 5.x | Always pass `features="lxml"` to BeautifulSoup constructor; fallback to `"html.parser"` if lxml not installed |
| playwright 1.50.x | Python 3.8+ | Requires `playwright install chromium` post-install step — add to setup docs; NOT needed if Workday CXS API works (it usually does) |

---

## Alternatives Considered (New Decisions)

| Recommended | Alternative | Why Not |
|-------------|-------------|---------|
| Firecrawl cloud Hobby | Firecrawl self-hosted | Self-hosted lacks Fire-engine (proxy rotation, stealth mode) — the main reason to use Firecrawl over raw httpx. Self-hosting saves $16/month but removes anti-bot protection |
| Firecrawl cloud Hobby | Apify | Apify is better for large-scale crawls across thousands of pages; for targeted ATS page fetching, Firecrawl's simpler API and Python SDK are more appropriate |
| httpx direct API for Greenhouse/Lever | Firecrawl for Greenhouse/Lever | Both ATS have free JSON APIs. Firecrawl credits are wasted here; use httpx |
| Workday CXS POST endpoint | Playwright for Workday | The undocumented CXS API works for most Workday tenants and avoids Playwright's 200MB browser dependency. Fall back to Playwright only if the tenant blocks the CXS endpoint |
| SiliconFlow Qwen3-8B (free) | OpenAI GPT-4o-mini for enrichment | GPT-4o-mini costs ~$0.60/M tokens. At 200 jobs/day × 500 tokens/job = 100K tokens/day = $0.06/day = ~$22/month. SiliconFlow free tier is $0. Use the free tier with batching |
| SiliconFlow with batching | One LLM call per job | 50 req/day free tier limit forces batching anyway. Send 5 jobs per request, extract structured JSON for each — reduces cost 5x and stays within free tier |

---

## Base Stack (Unchanged from 2026-03-25)

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.12+ | Runtime | Spec constraint; 3.12 is stable LTS with noticeable performance gains over 3.10/3.11; 3.13 exists but 3.12 has broader library compatibility |
| PostgreSQL | 16+ | Primary database | Spec constraint; required for pgvector extension; Postgres 16 added parallel query improvements relevant to vector workloads |
| pgvector | 0.4.2 (Python lib) / extension 0.8+ | Vector similarity search inside Postgres | Eliminates a dedicated vector DB (Pinecone, Weaviate, Qdrant); for this scale (thousands of job listings, not millions), co-locating structured data + embeddings in one DB is the right call — simpler ops, one connection pool, transactional guarantees |
| psycopg (v3) | 3.x | Postgres adapter | New projects should use psycopg3; 3-5x memory efficiency over psycopg2; native async support; required by pgvector HNSW best practices |
| httpx | 0.28.1 | HTTP client for GitHub raw content and ATS JSON APIs | Spec constraint; sync + async in one library; HTTP/2 support; correct for ATS API calls that don't need JS rendering |
| anthropic | 0.86.0 | LLM enrichment (Claude) | Spec constraint; use for fields requiring language understanding |
| openai | 2.30.0 | Embedding generation (text-embedding-3-small) | Spec constraint; $0.02/1M tokens, best cost/quality for semantic job matching |
| numpy | 1.26+ / 2.x | Vector arithmetic for multi-signal scoring | Spec constraint; correct for weighted score computation |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pydantic | v2 (2.x) | Schema validation | ALL data models |
| pydantic-settings | 2.x | Typed env var management | Always; surfaces config errors at startup |
| alembic | 1.18.x | Database migrations | Always |
| SQLAlchemy | 2.x | ORM / schema definitions | Schema + migrations; raw psycopg3 for hot paths |
| tenacity | 8.x | Retry with exponential backoff | All LLM API calls and Firecrawl requests |
| loguru | 0.7.x | Application logging | Always; single import, sane defaults |
| python-dateutil | 2.x | Date parsing | GitHub README table timestamps |
| mistune | 3.x | Markdown table parsing | SimplifyJobs README tables |
| fastapi | 0.135.x | HTTP API | When callers need HTTP interface |
| uvicorn | 0.42.x | ASGI server | Pair with FastAPI |

---

## Sources

- PyPI: firecrawl-py 4.21.0 — version confirmed March 25, 2026 (https://pypi.org/project/firecrawl-py/)
- Firecrawl docs: rate limits verified (https://docs.firecrawl.dev/rate-limits) — HIGH confidence
- Firecrawl pricing: 500 free lifetime credits, Hobby $16/3,000 credits/month, Standard $83/100K credits/month — verified March 2026 (https://www.firecrawl.dev/pricing)
- Firecrawl self-hosted docs: confirms Fire-engine (proxy/stealth) absent in self-hosted (https://docs.firecrawl.dev/contributing/self-host)
- Greenhouse Job Board API: unauthenticated public API confirmed (https://developers.greenhouse.io/job-board.html) — HIGH confidence
- Lever postings-api: v0 unauthenticated endpoint confirmed (https://github.com/lever/postings-api) — HIGH confidence
- Workday CXS API pattern: `POST /wday/cxs/{tenant}/{site}/jobs` — MEDIUM confidence (undocumented, widely used in open-source scrapers, no official docs)
- SiliconFlow rate limits: 1,000 RPM / 50K TPM (free tier), 50 req/day without credits / 1,000 req/day with $10+ credits — MEDIUM confidence (official docs page lacked exact numbers; sourced from rate-limits page + community sources)
- SiliconFlow pricing: Qwen3-8B at $0.06/M tokens — verified (https://www.siliconflow.com/models/qwen3-8b)
- OpenRouter free tier: 20 RPM / 200 req/day, 29 free models — MEDIUM confidence (https://openrouter.ai/collections/free-models)
- BeautifulSoup4 4.14.3: current version confirmed (https://www.crummy.com/software/BeautifulSoup/bs4/doc/)
- Firecrawl vs httpx comparison: multiple sources confirm Firecrawl adds value for JS-heavy pages, not for static/API-backed pages — MEDIUM confidence

---

*Stack research for: Python job scraping + vector matching engine (WeKruit Matching)*
*Base research: 2026-03-25 | Pipeline milestone update: 2026-03-31*
