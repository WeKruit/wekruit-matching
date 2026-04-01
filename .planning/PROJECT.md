# WeKruit Matching Engine

## What This Is

A Python service that scrapes, enriches, stores, and serves intern and new-grad job data for WeKruit. It powers the job corpus, matching APIs, and the shipped jobs console for browsing active/stale inventory, stats, and pipeline health.

## Core Value

People monitoring the WeKruit job corpus can immediately understand what jobs exist, what changed, and whether the pipeline is healthy.

## Current State

- Backend foundation and matching pipeline from phases 1-8 are shipped.
- v1.1 Internal UI Foundation is shipped: Jobs, Stale, Stats, and Pipeline now share one shell, one token system, clearer semantics, and a responsive jobs browsing experience.
- The next active milestone is v1.2 Job Data Pipeline.

## Current Milestone: v1.2 Job Data Pipeline

**Goal:** Expand the job data pipeline so each listing can carry richer job-description text, structured ATS fetch provenance, and better observability without disrupting the current daily update flow.

**Target features:**
- DB migration for JD fetch tracking and ATS content hashes
- URL classifier for Greenhouse, Lever, Ashby, Workday, and Firecrawl fallback
- Free ATS parsers plus Firecrawl integration for the long tail
- Stage 2b enrichment orchestrator wired into the daily pipeline
- Pipeline observability, email digest, and 1K-job end-to-end verification

## Requirements

### Validated

- ✓ Daily scrape, enrich, and embed pipeline populates the jobs corpus — v1.0
- ✓ Jobs lifecycle supports active and inactive/stale listings — v1.0
- ✓ Matching and stats endpoints expose job inventory data to consumers — v1.0
- ✓ v1.1 jobs console shell, tokens, accessibility, responsive jobs browsing, and stats/pipeline hierarchy — v1.1

### Active

- [ ] Track JD fetch attempts and sources without disturbing existing content-hash behavior
- [ ] Route each job URL to the correct ATS or Firecrawl fetch strategy with no network I/O in classification
- [ ] Fetch Greenhouse, Lever, Ashby, and Workday job descriptions into normalized plain text
- [ ] Insert JD enrichment into the daily pipeline as a new Stage 2b
- [ ] Expose JD coverage, queue depth, and data quality through the pipeline surface and email summaries

### Out of Scope

- UI redesign beyond the shipped v1.1 console foundation
- Matching logic changes and recommender experimentation
- VALET, desktop, onboarding, or billing work
- Full authentication / customer account model for the jobs console

## Context

- **UI state:** `src/wekruit_matching/api/internal_ui.py` now contains the shipped SSR console with shared shell and tokenized styling.
- **Current pages:** `/internal/jobs`, `/internal/jobs?status=inactive`, `/internal/stats`, `/internal/pipeline`
- **Pipeline milestone references:** Firecrawl research, ATS parser feasibility, and staged rollout constraints are already captured in roadmap/requirements.
- **Operational constraint:** The existing daily scrape/enrich/embed flow must keep running while JD enrichment is added as a separate stage.

## Constraints

- **Tech stack**: Python + FastAPI + server-rendered HTML for UI; Postgres/pgvector for data
- **Scope**: v1.2 is data-pipeline work, not another UI milestone
- **Stability**: `enrich_from_jobright.py`, `cron_scraper.sh`, and existing matching behavior should remain stable during JD pipeline expansion
- **Cost**: Prefer free ATS APIs before Firecrawl, and prevent repeated credit spend through fetch tracking

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Keep the console server-rendered | Fastest path to a strong UI without introducing a second frontend architecture | ✓ Good |
| Model internal and external as surface modes on one shell | Prevents duplicated page trees and preserves one design system | ✓ Good |
| Keep v1.2 separate from the UI milestone | Prevents pipeline expansion from muddying the UI delivery scope | ✓ Good |

---
*Last updated: 2026-03-31 after v1.1 milestone completion*
