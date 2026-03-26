# WeKruit Matching Engine

## What This Is

A standalone backend that scrapes intern and new grad job listings from SimplifyJobs GitHub repos, stores them in a database with LLM-enriched metadata and embeddings, and returns ranked job matches based on user profiles. No frontend — any client (Discord bot, web app, API) can consume the matching API.

## Core Value

Given a user profile with skills, preferences, and career goals, return the most relevant job listings ranked by fit — so users don't waste time scrolling through hundreds of irrelevant posts.

## Requirements

### Validated

(None yet — ship to validate)

### Active

- [ ] Scraper pulls job listings from SimplifyJobs GitHub README tables (intern + new grad)
- [ ] Scraper handles closed listings (skip rows with lock emoji)
- [ ] Scraper generates stable IDs for deduplication
- [ ] LLM enrichment classifies industry, company size, skills, sponsorship per job
- [ ] Embedding generation for semantic matching (text-embedding-3-small)
- [ ] Database stores jobs with enriched metadata and vector embeddings
- [ ] Upsert logic: insert new, update existing, mark stale as inactive
- [ ] User profile schema with preferences, skills, experience, feedback history
- [ ] Matching engine scores jobs using weighted multi-signal scoring (title similarity, skills overlap, industry, company size, location, recency, feedback boost)
- [ ] Hard filters: job type, sponsorship requirement, location preferences
- [ ] Fuzzy location matching with normalization (SF/San Francisco, NYC/New York, etc.)
- [ ] Feedback loop: like/dislike updates user preferences and affinity embedding
- [ ] Cron-ready scraper and enrichment scripts
- [ ] End-to-end test script demonstrating full pipeline

### Out of Scope

- Discord bot integration — separate spec exists, deferred
- Daily digest delivery (DM formatting)
- Resume parsing
- Web dashboard
- Email notifications
- User authentication — caller provides user profile directly

## Context

- **Data sources:** SimplifyJobs/Summer2026-Internships and SimplifyJobs/New-Grad-Positions GitHub repos (raw README markdown tables)
- **Additional web sources:** intern-list.com and newgrad-jobs.com (same underlying data)
- **Target users:** WeKruit's user base — students and new grads looking for tech jobs
- **Noah's directive:** "Give me something I can use" — ship a working backend, not a prototype
- **Enrichment approach:** LLM (Claude/OpenAI) classifies metadata not in the table; embeddings via text-embedding-3-small
- **Matching weights:** title_similarity 0.30, skills_overlap 0.25, industry_match 0.15, company_size_match 0.10, location_fit 0.10, recency 0.05, feedback_boost 0.05

## Constraints

- **Stack**: Python 3.12+, Postgres with pgvector, httpx, numpy
- **LLM APIs**: Anthropic (enrichment) + OpenAI (embeddings)
- **No frontend**: Pure backend — API/library interface only
- **Database**: Postgres with pgvector extension for vector similarity search
- **Cost**: Minimize LLM calls — only enrich new/changed jobs, cache embeddings

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Scrape GitHub README instead of websites | Structured markdown, no browser needed, stable format | -- Pending |
| Postgres + pgvector over standalone vector DB | Simpler stack, one DB for structured + vector data | -- Pending |
| Weighted multi-signal scoring over pure embedding similarity | Captures explicit preferences (location, sponsorship) that embeddings miss | -- Pending |
| text-embedding-3-small over larger models | Good quality/cost ratio for job matching use case | -- Pending |
| No auth layer | Engine is a library — auth belongs in the frontend layer | -- Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? Move to Out of Scope with reason
2. Requirements validated? Move to Validated with phase reference
3. New requirements emerged? Add to Active
4. Decisions to log? Add to Key Decisions
5. "What This Is" still accurate? Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-03-25 after initialization*
