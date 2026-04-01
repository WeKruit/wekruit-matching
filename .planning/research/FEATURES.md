# Feature Research

**Domain:** Job data collection pipeline (Firecrawl scraping, ATS parsing, structured JD extraction)
**Researched:** 2026-03-31
**Confidence:** HIGH (Firecrawl capabilities, ATS URL patterns), MEDIUM (data quality scoring heuristics), MEDIUM (competitor methodology)

---

## Context: Subsequent Milestone

This document extends the original FEATURES.md (2026-03-25) which covered the matching engine core. This research covers the **new data collection milestone** only: fetching full job description pages via Firecrawl, parsing ATS-structured pages, extracting structured JD fields, and scoring data quality. The matching engine features from the original file remain in force and are not repeated here.

**Existing capabilities (already built, not researched here):**
- GitHub README scraping (SimplifyJobs repos)
- LLM enrichment via Qwen3-8B (industry/skills from title only)
- OpenAI embeddings + 7-signal weighted scoring
- Internal admin UI at matching.wekruit.com/internal/*
- Mailgun pipeline email notifications

---

## Firecrawl API Capabilities (Verified)

Firecrawl provides four endpoints relevant to this milestone. All verified against official docs.

| Endpoint | What It Does | Cost | When to Use |
|----------|-------------|------|-------------|
| `/scrape` | Fetches a single URL, returns markdown/JSON/HTML/screenshot | 1 credit base; +4 credits for JSON mode | Per-job ATS page fetch with structured extraction |
| `/batch-scrape` | Async scrape of up to 5,000 URLs per job; returns job ID for polling | Same per-URL cost; expires after 24h | Bulk enrichment runs across many employer URLs |
| `/search` | Web search + optional scrape of results in one call | Per-result scrape costs apply | Discovering employer career page URLs by company name |
| `/extract` (v2 `/extract`) | Multi-URL or wildcard crawl with schema-guided LLM extraction | Per-URL + LLM processing | When employer URL is known but page structure is unpredictable |

**JSON mode (on `/scrape`):** Pass `formats: ["json"]` with a JSON Schema (or Pydantic-compatible schema) and Firecrawl runs LLM extraction inline. Returns `data.json` matching the schema. Costs 4 additional credits per page. Use this for ATS pages where field layout is consistent but HTML structure varies.

**Actions parameter:** Supports `click`, `write`, `press`, `wait`, `screenshot` actions before scraping. Enables scraping pages behind "Load More" buttons, modal dialogs, or login flows. Relevant for Workday (requires JavaScript-heavy navigation) and any custom career portal.

**Batch scrape async pattern (Python SDK):**
```python
job = app.async_batch_scrape_urls(urls, params={"formats": ["json"], "jsonOptions": {"schema": schema}})
# Poll via job.id
status = app.get_batch_scrape_status(job.id)
```
Python SDK: `firecrawl-py` on PyPI. Confirmed supports batch async as of SDK 1.4.x.

**Search + scrape combined:**
```python
results = app.search("Google software engineer internship site:jobs.lever.co", scrape_options={"formats": ["json"]})
```
Returns web search results with scraped content attached. Useful for discovering direct employer ATS URLs when only company + role name is known.

---

## ATS Platform Patterns (Verified)

The following ATS platforms cover the majority of tech internship and new grad postings. URL patterns are stable and reliably identify the platform from a job URL alone.

### Greenhouse
- **URL pattern:** `boards.greenhouse.io/{company}` or `job-boards.greenhouse.io/{company}/jobs/{id}`
- **Public API:** `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs` — no auth required
- **Fields from API (no scraping needed):** `id`, `title`, `location.name`, `absolute_url`, `updated_at`, `requisition_id`
- **Optional params:** `?content=true` adds HTML job description; `?questions=true` adds application form fields; `?pay_transparency=true` adds `pay_input_ranges` (salary range)
- **Departments/offices:** included when `?content=true`
- **What's missing from API:** explicit skills list, seniority level, tech stack — must extract from `content` HTML via LLM
- **Confidence:** HIGH — official Greenhouse Job Board API docs verified

### Lever
- **URL pattern:** `jobs.lever.co/{company}/{posting_id}` or `posting.lever.co/{company}`
- **Public API:** `GET https://api.lever.co/v0/postings/{clientname}` — no auth required
- **Fields from API:** `id`, `text` (title), `hostedUrl`, `applyUrl`, `description` (HTML), `descriptionPlain`, `lists` (requirements/benefits as structured arrays), `categories` (location, commitment, team, department), `salaryRange` (currency, interval, min, max), `workplaceType` (on-site/remote/hybrid), `country` (ISO 3166-1)
- **Notable:** `lists` is the richest structural field — Lever separates requirements, responsibilities, and benefits as named arrays, not one blob. Parse these individually.
- **What's missing:** explicit skills taxonomy, tech stack list — extract from `description` and `lists` via LLM
- **Confidence:** HIGH — official Lever postings-api GitHub docs verified

### Workday
- **URL pattern:** `{tenant}.wd{N}.myworkdayjobs.com/{locale}/{site}` — N varies (1, 3, 5 etc. per company's data center)
- **No public API:** must use undocumented internal API
- **Jobs list:** `POST /wday/cxs/{tenant}/{site}/jobs` — body: `{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}`
- **Job detail:** `GET /wday/cxs/{tenant}/{site}/job/{externalPath}`
- **Available fields:** `jobId`, `title`, `locationsText`, `country`, `countryCode`, `employmentType`, `remoteType`, `jobReqId`, `description` (HTML), `applyUrl`
- **Key complexity:** POST required for listing (not GET); tenant and data center number must be determined from company's actual careers page URL — cannot be inferred; JavaScript-heavy, may need Firecrawl actions
- **Salary:** Sometimes available in description or as structured field depending on state disclosure laws
- **Confidence:** MEDIUM — documented via community reverse-engineering (Apify actors, GitHub crawlers); no official API docs

### Ashby
- **URL pattern:** `jobs.ashbyhq.com/{company}` (growing adoption at Series A/B startups)
- **Public API:** `GET https://api.ashbyhq.com/posting-api/job-board/{clientname}?includeCompensation=true`
- **Fields:** `title`, `location`, `secondaryLocations`, `department`, `team`, `isRemote`, `workplaceType`, `descriptionHtml`, `descriptionPlain`, `publishedAt`, `employmentType`, `address`, `jobUrl`, `applyUrl`, `compensationTierSummary`, `scrapeableCompensationSalarySummary`
- **Best compensation coverage:** Ashby's `includeCompensation=true` flag and `scrapeableCompensationSalarySummary` field provide better salary data than Greenhouse or Lever
- **Confidence:** HIGH — official Ashby developer docs verified

### Other ATS Platforms (Detected by URL, Lower Priority)

| Platform | URL Pattern | Notes |
|----------|-------------|-------|
| SmartRecruiters | `careers.smartrecruiters.com/{company}` | Has public job listing API |
| BambooHR | `{company}.bamboohr.com/careers` | No public API; scrape required |
| Jobvite | `jobs.jobvite.com/{company}` | XML/JSON feed available at `/feeds/jobs.json` |
| Rippling | `ats.rippling.com/{company}` | Newer; scraping only |
| Taleo | `{company}.taleo.net` | Enterprise; complex, often requires JS |
| iCIMS | `careers.icims.com/jobs/{company}` | Has partial XML feed |

---

## Feature Landscape

### Table Stakes (Users Expect These)

Features that any job data collection pipeline must have. Without these, the enriched data is either incomplete, stale, or unreliable for matching.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Employer URL resolution | SimplifyJobs provides a `url` field pointing to the actual ATS page; fetching it is required to get full JD content beyond title/company/location | LOW | The URL is already in `listings.json`; this is just "follow the link" — httpx for Greenhouse/Lever/Ashby APIs, Firecrawl for HTML-heavy pages |
| ATS platform detection from URL | Must route each URL to the correct parser (API vs. scrape vs. Workday POST pattern) | LOW | Regex match on URL host: `boards.greenhouse.io`, `jobs.lever.co`, `jobs.ashbyhq.com`, `*.myworkdayjobs.com`; maintain a platform routing table |
| Full job description extraction | Title + company + location alone are insufficient for skills matching; full JD text needed for LLM enrichment and embedding | MEDIUM | For Greenhouse/Lever/Ashby: use public API `?content=true`; for Workday + custom pages: Firecrawl `/scrape` with markdown format |
| Structured field extraction (title, company, location, employment type, description) | Downstream matching requires structured fields, not a markdown blob | MEDIUM | Greenhouse/Lever/Ashby APIs return these natively; Workday + custom pages need Firecrawl JSON mode with schema |
| Incremental fetch (only new/changed jobs) | Fetching all employer URLs on every run wastes Firecrawl credits and LLM budget | MEDIUM | Track `last_fetched_at` and `content_hash` per job; skip if hash unchanged. For ATS API platforms, compare `updated_at` field before fetching content |
| Retry with backoff on fetch failures | Network failures and rate limits are guaranteed; silent drops break the pipeline | LOW | Use `tenacity` (already in stack) with exponential backoff + jitter; 3 retries, 2-30s window; log failures to separate table for manual review |
| Deduplication across sources | Same job may appear in SimplifyJobs README AND via direct employer URL; must not create two records | MEDIUM | Canonical ID = hash of `(company_name_normalized + title_normalized + ats_platform + ats_job_id)`; ATS job IDs are stable within a platform |

### Differentiators (Competitive Advantage)

Features that materially improve matching quality beyond what a naive scraper provides. These are what distinguish the WeKruit engine from raw SimplifyJobs data.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Structured JD extraction via Firecrawl JSON mode | Extract skills, requirements, responsibilities, compensation as typed fields — not a text blob — which enables per-field matching signals | HIGH | Define Pydantic schema: `{required_skills: list[str], preferred_skills: list[str], responsibilities: list[str], min_experience_years: int, seniority: str, employment_type: str, salary_min: int, salary_max: int, salary_currency: str, remote_policy: str, visa_sponsorship: bool, tech_stack: list[str]}`; pass to Firecrawl `/scrape` JSON mode or Firecrawl `/extract`; Costs 4 credits/page above base |
| Firecrawl `/search` for employer URL discovery | When SimplifyJobs has a company name but broken/missing URL, use Firecrawl search to find the ATS page directly | MEDIUM | Query: `"{company_name} {job_title} site:jobs.lever.co OR site:boards.greenhouse.io OR site:jobs.ashbyhq.com"`; attach `scrapeOptions` to pull structured data in same call; avoids a second round-trip |
| Salary range extraction and normalization | Compensation data from Ashby (native), Lever (`salaryRange`), Greenhouse (`pay_input_ranges`), and Workday (description extraction) enables a salary filter that wasn't viable before | MEDIUM | Normalize: strip currency symbol, convert to annual ($80K/mo → $960K/yr is wrong; detect interval), store as `{min_usd_annual, max_usd_annual, currency, pay_period}`; flag as `salary_confidence: [exact, extracted, inferred, missing]` |
| Data quality scoring per job record | Enables the matching engine to down-weight low-quality records (missing description, stale posting, incomplete fields) | MEDIUM | Score 0-100: completeness (50 pts) + recency (25 pts) + description length (15 pts) + salary presence (10 pts); store as `data_quality_score` column; use as a soft signal in matching or a hard filter threshold |
| ATS-native structured fields over LLM inference | Greenhouse departments, Lever `lists` (requirements/benefits/responsibilities as separate arrays), Ashby compensation — these are already parsed by the ATS; using them is more reliable and cheaper than LLM extraction | LOW | For each ATS platform, map native API fields to the canonical schema first; only invoke LLM extraction for fields not natively available or for custom/unknown career pages |
| Visa sponsorship signal from JD text | Students on visas need this as a hard filter; Workday and custom pages often mention it explicitly in the description | MEDIUM | Already flagged as differentiator in prior research; now expanded: with full JD text available (not just title), sponsorship detection accuracy improves significantly. Patterns: "unable to sponsor", "must be authorized to work", "H-1B", "OPT/CPT eligible" |
| Tech stack extraction as separate field | Matching against a `tech_stack` list is more signal-dense than fuzzy skill matching on a combined description blob | HIGH | Firecrawl JSON mode schema includes `tech_stack: list[str]`; supplement with regex for known tech tokens (Python, React, Kubernetes, etc.) as a low-cost fallback for simple pages |
| Ghost posting detection | Many listings remain open after position is filled; down-weighting or filtering these improves user experience | MEDIUM | Heuristics: `last_updated > 60 days`, no changes to description hash across 3 scrape cycles, posting date > 90 days ago with no activity; set `suspected_ghost: true` flag; do not hard-delete |

### Anti-Features (Commonly Requested, Often Problematic)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Full site crawl of employer careers pages | "Get all jobs from Google's careers page" | Firecrawl `/crawl` on a large careers site can burn hundreds of credits and hit rate limits; most large employers (Google, Meta, Amazon) have anti-bot protections that block even Firecrawl's proxied requests; ToS risk | Use ATS public APIs (Greenhouse/Lever/Ashby) for structured listing retrieval; fall back to Firecrawl `/scrape` for individual known URLs only, not wildcard crawls |
| Scraping LinkedIn or Indeed | "These have more jobs" | LinkedIn's anti-scraping is aggressive and legally contested; Indeed TOS prohibits scraping; both block standard scrapers and Firecrawl; account bans and legal risk are real | Stick to employer-direct ATS pages and SimplifyJobs as the aggregation layer; LinkedIn data is available via their official Jobs API (requires partnership) |
| Storing full HTML or raw markdown of JD | "Keep everything for re-processing" | Full HTML/markdown is large (5-50KB per job), doubles storage cost, and rarely needed after structured extraction; re-processing from raw HTML is brittle as pages change | Store extracted structured fields + `description_plain` text only; re-fetch if needed (Firecrawl caches by default for 2 days via `maxAge` param) |
| Real-time ATS polling (sub-hourly) | "Get jobs the moment they're posted" | SimplifyJobs (the upstream source) updates ~daily via GitHub Actions; polling employer ATS pages faster than that wastes Firecrawl credits; most ATS platforms throttle frequent clients | Align fetch cadence to SimplifyJobs update frequency; run enrichment pipeline after each successful GitHub scrape, not independently |
| Paying for Firecrawl LLM extraction on every scrape | "Always use JSON mode for best quality" | JSON mode costs 5 credits/page (1 base + 4 JSON); for Greenhouse, Lever, Ashby jobs where the API already returns structured fields, this is waste | Only invoke Firecrawl JSON mode for Workday and custom/unknown career pages; use native ATS API fields everywhere else |
| Generalizing to non-tech job postings | "Support all industries" | The current skill taxonomy, seniority detection, and tech stack patterns are calibrated for software engineering roles; generalization dilutes match quality for the target user base (students, new grads in tech) | Stay focused on tech roles; the SimplifyJobs source already scopes this appropriately |

---

## Feature Dependencies

```
[SimplifyJobs GitHub Scraper] (existing)
    └──produces──> [Job Records with URL field]
                       └──feeds──> [ATS Platform Detection]
                                       ├──routes to──> [Greenhouse API Fetch]
                                       ├──routes to──> [Lever API Fetch]
                                       ├──routes to──> [Ashby API Fetch]
                                       ├──routes to──> [Workday POST Scrape]
                                       └──routes to──> [Firecrawl Scrape (unknown/custom)]

[ATS Platform Detection]
    └──requires──> [Employer URL] (from listings.json or search discovery)

[Firecrawl Search Discovery]
    └──produces──> [Employer URL]
    └──requires──> [Company Name + Job Title] (from existing scraper output)

[Full JD Fetch] (all routes above converge here)
    └──produces──> [Raw JD Content (description_plain, structured_fields)]
    └──feeds──> [Incremental Hash Check]
                    └──gates──> [Structured JD Extraction]
                                    └──requires──> [Firecrawl JSON Mode] (Workday/custom)
                                    └──requires──> [ATS Native Fields Mapping] (GH/Lever/Ashby)
                                    └──produces──> [Canonical JD Schema Record]

[Canonical JD Schema Record]
    ├──feeds──> [Salary Normalization]
    ├──feeds──> [Visa Sponsorship Detection] (enhanced with full text)
    ├──feeds──> [Tech Stack Extraction]
    ├──feeds──> [Data Quality Scoring]
    └──feeds──> [LLM Enrichment Pipeline] (existing — now has richer input)

[Data Quality Scoring]
    └──requires──> [Canonical JD Schema Record]
    └──enhances──> [Weighted Multi-Signal Scoring] (existing — quality score as soft signal)

[Ghost Posting Detection]
    └──requires──> [Multiple scrape cycles] (needs historical hash comparison)
    └──requires──> [Canonical JD Schema Record] (last_updated, date_posted)
```

### Dependency Notes

- **ATS Platform Detection must run before any fetch:** The fetch strategy (API call vs. Firecrawl scrape vs. Workday POST) depends entirely on which ATS is detected. This is a router, not optional middleware.
- **Firecrawl Search Discovery is a fallback, not primary:** Primary path is "follow URL from listings.json". Search discovery only activates when URL is missing, broken (4xx), or redirects to a homepage.
- **Incremental hash check gates the expensive steps:** Structured JD extraction (Firecrawl JSON mode) and LLM enrichment should only run when content has changed. Hash the `description_plain` field; if unchanged, skip downstream.
- **Data Quality Scoring requires the complete canonical record:** Score is computed after all extraction; cannot be computed mid-pipeline.
- **Ghost Posting Detection requires multiple cycles:** Cannot be computed on first fetch; needs at least 2-3 scrape cycles to establish a "no change" pattern.

---

## Canonical JD Schema (Target Fields Per Job Record)

These are the fields the extraction pipeline must produce. Confidence levels reflect how reliably each ATS surfaces the field.

| Field | Type | Source | Confidence | Matching Value |
|-------|------|--------|------------|----------------|
| `title` | string | All ATS APIs + scrape | HIGH | P1 signal |
| `company_name` | string | SimplifyJobs + ATS | HIGH | Dedup key |
| `location` | string | All ATS APIs | HIGH | `location_fit` signal |
| `remote_policy` | enum(onsite/remote/hybrid) | Lever `workplaceType`, Ashby `isRemote`, Workday `remoteType` | HIGH | Hard filter candidate |
| `employment_type` | enum(fulltime/parttime/internship/contract) | All ATS APIs | HIGH | Hard filter |
| `description_plain` | text | All sources with `?content=true` or Firecrawl markdown | HIGH | Base for LLM enrichment + embedding |
| `required_skills` | list[str] | LLM extract from description + Lever `lists` | MEDIUM | `skills_overlap` signal |
| `preferred_skills` | list[str] | LLM extract from description | MEDIUM | Soft scoring |
| `tech_stack` | list[str] | Firecrawl JSON mode + regex fallback | MEDIUM | `skills_overlap` signal |
| `seniority_level` | enum(intern/entry/mid/senior) | LLM extract from title + description | MEDIUM | Hard filter candidate |
| `min_experience_years` | int | LLM extract from requirements | MEDIUM | Soft filter |
| `salary_min_usd` | int | Lever `salaryRange`, Ashby `compensationTierSummary`, Greenhouse `pay_input_ranges` | LOW (inconsistent) | Future: salary filter |
| `salary_max_usd` | int | Same as above | LOW (inconsistent) | Future: salary filter |
| `salary_confidence` | enum(exact/extracted/inferred/missing) | Derived | HIGH (as metadata) | Data quality |
| `visa_sponsorship` | enum(yes/no/unknown) | LLM classify from description_plain | MEDIUM | Hard filter |
| `date_posted` | datetime | All ATS (Lever, Ashby `publishedAt`, Greenhouse `updated_at`) | HIGH | Recency signal |
| `ats_platform` | enum | URL detection | HIGH | Routing / debug |
| `ats_job_id` | string | Platform-native ID field | HIGH | Dedup key |
| `data_quality_score` | int (0-100) | Computed: completeness + recency + desc_length + salary | HIGH (formula) | Down-weight low quality |
| `last_fetched_at` | datetime | Pipeline metadata | HIGH | Freshness tracking |
| `content_hash` | string | SHA-256 of description_plain | HIGH | Incremental check gate |

---

## Data Quality Scoring Formula

Heuristic score for each job record. Stored as `data_quality_score` (0–100). Used as a soft down-weight in matching and as a hard filter threshold for admin review.

```
data_quality_score =
  completeness_score (0-50)
    + recency_score (0-25)
    + description_length_score (0-15)
    + salary_score (0-10)

Where:
  completeness_score  = 10 per present field: {title, company, location, employment_type, description_plain, seniority_level, remote_policy}
                        (5 fields required for 50pts max; penalize missing)
  recency_score       = 25 if posted <= 14 days ago
                        15 if posted 15-30 days ago
                        5  if posted 31-60 days ago
                        0  if posted > 60 days ago or date_posted missing
  description_length  = 15 if len(description_plain) > 500 chars
                        8  if 200-500 chars
                        0  if < 200 chars
  salary_score        = 10 if salary_confidence == "exact"
                        5  if salary_confidence == "extracted"
                        0  if "inferred" or "missing"
```

**Confidence:** MEDIUM — formula is a heuristic; weights are reasonable starting points but should be tuned once real data is available.

---

## MVP Definition

### Launch With (v1 — Job Data Collection Pipeline)

Minimum viable for the new data collection milestone. Goal: replace title-only LLM enrichment with full JD text, and add structured field extraction.

- [ ] ATS platform detection from URL (route table for Greenhouse/Lever/Ashby/Workday/unknown) — everything else depends on this
- [ ] Greenhouse API fetch (`?content=true`) — largest share of SimplifyJobs listings
- [ ] Lever API fetch (native structured fields + description) — second most common
- [ ] Firecrawl `/scrape` in markdown mode for Workday + unknown platforms — covers the long tail
- [ ] Incremental hash check before fetch — cost control; prevents re-fetching unchanged JDs
- [ ] Canonical JD schema mapping (ATS native fields → canonical schema) — unifies output across platforms
- [ ] `description_plain` stored and passed to existing LLM enrichment pipeline — immediate improvement to enrichment quality
- [ ] Retry with backoff on fetch failures — operational reliability
- [ ] `data_quality_score` computation — enables downstream filtering

### Add After Validation (v1.x)

Add once core pipeline is running and producing quality descriptions.

- [ ] Firecrawl JSON mode schema extraction (structured fields from description) — trigger: description_plain is flowing; now extract typed fields
- [ ] Salary normalization and `salary_confidence` flag — trigger: enough platforms return salary to make it useful
- [ ] Visa sponsorship re-classification using full description text — trigger: full JD text available; accuracy should improve significantly
- [ ] Ashby API fetch — trigger: measure what % of SimplifyJobs URLs are Ashby; add if significant
- [ ] Firecrawl `/search` employer URL discovery — trigger: measure broken URL rate in SimplifyJobs; add if > 10%
- [ ] Ghost posting detection — trigger: after 2-3 weeks of pipeline history exists

### Future Consideration (v2+)

- [ ] Tech stack extraction as separate column — defer until skills matching quality is validated
- [ ] Additional ATS platforms (SmartRecruiters, Jobvite, BambooHR) — defer until coverage data shows gap
- [ ] Salary filter in matching engine — defer until salary data coverage > 30% of jobs

---

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| ATS platform detection | HIGH | LOW | P1 |
| Greenhouse API fetch + description | HIGH | LOW | P1 |
| Lever API fetch + description | HIGH | LOW | P1 |
| Firecrawl scrape (Workday + unknown) | HIGH | MEDIUM | P1 |
| Incremental hash check | HIGH | LOW | P1 |
| Canonical JD schema mapping | HIGH | MEDIUM | P1 |
| Data quality scoring | MEDIUM | LOW | P1 |
| Retry with backoff | HIGH | LOW | P1 |
| Firecrawl JSON mode extraction | HIGH | HIGH | P2 |
| Salary normalization | MEDIUM | MEDIUM | P2 |
| Visa sponsorship re-classification | HIGH | LOW | P2 |
| Ashby API fetch | MEDIUM | LOW | P2 |
| Firecrawl search URL discovery | MEDIUM | MEDIUM | P2 |
| Ghost posting detection | MEDIUM | MEDIUM | P3 |
| Tech stack extraction column | MEDIUM | HIGH | P3 |
| Additional ATS platforms | LOW | HIGH | P3 |

---

## Competitor Feature Analysis

How SimplifyJobs, JobRight, and LinkedIn collect JD data — what we can learn from each.

| Feature | SimplifyJobs | JobRight | LinkedIn | WeKruit Pipeline |
|---------|-------------|----------|----------|-----------------|
| Data source | GitHub repos (structured markdown tables) | GitHub repos (same data) | Direct employer integrations + user submissions | SimplifyJobs → employer ATS pages via Firecrawl/API |
| Full JD text | No (title + company + URL only in README) | No (same README source) | Yes (employer-submitted) | Yes (via ATS API + Firecrawl) |
| Structured fields (skills, salary, seniority) | No | No | Partial (employer-provided) | Yes (extracted via Firecrawl JSON mode + ATS native) |
| ATS detection | No | No | N/A (direct integration) | Yes (URL routing table) |
| Salary data | No | No | Partial (state-mandated disclosure) | Partial (Ashby/Lever/Greenhouse API fields where available) |
| Sponsorship flag | No | No | No | Yes (LLM classify from full JD text) |
| Ghost posting detection | Manual (lock emoji) | Same | Partial (LinkedIn removes expired) | Yes (hash + recency heuristic) |
| Data freshness | Daily (GitHub Actions) | Same | Near-real-time | Daily (aligned to SimplifyJobs cadence) |
| Data quality scoring | No | No | No | Yes (per-record heuristic score) |

**Key gap filled:** SimplifyJobs and JobRight are aggregation layers — they provide pointers (company + URL) but not full JD content. WeKruit bridges this by following URLs to ATS pages and extracting structured data, which materially improves LLM enrichment quality and enables new matching signals.

---

## Sources

- [Firecrawl Extract endpoint docs](https://docs.firecrawl.dev/features/extract) — URL formats, schema support, LLM extraction, `enableWebSearch` flag; HIGH confidence
- [Firecrawl Scrape endpoint docs](https://docs.firecrawl.dev/features/scrape) — JSON mode, actions, output formats, `maxAge` cache param; HIGH confidence
- [Firecrawl Search endpoint docs](https://docs.firecrawl.dev/features/search) — query params, combined search+scrape, `scrapeOptions`; HIGH confidence
- [Firecrawl Batch Scrape launch post](https://www.firecrawl.dev/blog/launch-week-ii-day-1-introducing-batch-scrape-endpoint) — async batch pattern, 5,000 URL limit, 24h expiry; HIGH confidence
- [Greenhouse Job Board API docs](https://developers.greenhouse.io/job-board.html) — all query params, field names, pay_input_ranges; HIGH confidence (official)
- [Lever postings-api GitHub README](https://github.com/lever/postings-api/blob/master/README.md) — full field listing including salaryRange, lists, workplaceType; HIGH confidence (official)
- [Ashby Job Postings API docs](https://developers.ashbyhq.com/docs/public-job-posting-api) — endpoint URL, compensation fields, `includeCompensation` param; HIGH confidence (official)
- [Workday scraping patterns (Apify)](https://apify.com/blackfalcondata/workday-scraper) and [GitHub community crawlers](https://github.com/chuchro3/WebCrawler) — POST endpoint pattern, tenant/site URL structure; MEDIUM confidence (community-sourced, not official)
- [wrkmatch ATS detection project](https://github.com/daviderubio/wrkmatch) — URL fingerprinting for Greenhouse, Lever, Ashby, Workable, Recruitee; HIGH confidence (working open-source implementation)
- [Hiring Signal Tracker (Apify)](https://apify.com/emastra/hiring-signal-tracker) — confirms Greenhouse/Lever/Ashby/Workday URL patterns are standard and stable; MEDIUM confidence
- [Data quality scoring (Clarity Scorecard)](https://www.cleansmartlabs.com/blog/how-to-measure-data-quality-building-a-clarity-scorecard) — completeness × recency × duplicate scoring formula; MEDIUM confidence
- [Job data normalization (jobspikr)](https://www.jobspikr.com/blog/job-data-normalization/) — deduplication, location normalization, title normalization patterns; MEDIUM confidence

---

*Feature research for: Job data collection pipeline (Firecrawl, ATS parsing, structured JD extraction)*
*Researched: 2026-03-31*
