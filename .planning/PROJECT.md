# WeKruit Platform

## What This Is

A multi-service job matching platform for interns and new grads. A Python pipeline on Mac Mini scrapes ~47K job listings daily, enriches them with structured data and vector embeddings, and syncs to Firebase. A TypeScript Cloud Functions service on Firebase serves the customer-facing matching API, job board, and outbound scheduling. VALET (Electron desktop app) owns user identity and profiles via Supabase Postgres.

## Core Value

New grads and interns find the best job matches for their skills and preferences through a personalized, filter-first matching engine — without scrolling through thousands of irrelevant posts.

## Current State

- v1.0 backend foundation: scrape, enrich, embed pipeline + matching API + FastAPI server
- v1.1 internal UI: jobs console with shared shell, tokens, responsive browsing
- v1.2 JD data pipeline: ATS parsers (Greenhouse/Lever/Ashby), Firecrawl self-hosted, URL resolution, pipeline observability
- Pipeline runs daily 6 AM CDT on Mac Mini (launchd), 47K active jobs, 97%+ coverage on JD/skills/sponsorship
- Firebase core service exists with outbound scheduling (Retell AI calls, Mailgun, Google Calendar)
- VALET has full user system in Supabase (profiles, resumes, preferences, EEO, auth)

## Current Milestone: v2.0 Platform Unification

**Goal:** Make Firebase Core Service the central hub for all customer-facing APIs — user sync from VALET Supabase, job sync from pipeline, matching engine on Cloud Functions, and job board API. One registration propagates across the entire WeKruit system.

**Target features:**
- Supabase DB Webhook → Firestore `/platform-users/{uid}` sync (< 1s, zero VALET code changes)
- Pipeline → Firestore `/matching-jobs/{jobId}` sync (diff-based, initial 47K load + daily incremental)
- Matching Cloud Function with filter-first approach (Firestore WHERE → cosine sim in-memory → 7-signal scorer)
- Job board API on Cloud Functions (browse, search, paginate, save, feedback)
- Collection prefix naming (`platform-`, `matching-`, `outbound-`) for service ownership
- Pipeline Mac Mini adds sync-to-Firebase step after daily run
- Comprehensive handoff doc with pipeline architecture, Mac Mini setup, Firecrawl Docker instructions

## Requirements

### Validated

- ✓ Daily scrape, enrich, and embed pipeline populates the jobs corpus — v1.0
- ✓ Jobs lifecycle supports active and inactive/stale listings — v1.0
- ✓ Matching and stats endpoints expose job inventory data to consumers — v1.0
- ✓ v1.1 jobs console shell, tokens, accessibility, responsive jobs browsing — v1.1
- ✓ JD fetch tracking, ATS parsers, Firecrawl, pipeline observability — v1.2

### Active

(Defined in REQUIREMENTS.md)

### Out of Scope

- Frontend job board UI (future phase, not this milestone's core)
- Firebase Auth migration (VALET keeps custom JWT for now)
- VALET code changes (sync is via Supabase DB Webhooks, zero VALET modifications)
- Matching algorithm changes (port existing 7-signal scorer as-is, optimize later)

## Context

- **Pipeline**: `wekruit-matching/` on Mac Mini — Python 3.12, FastAPI, psycopg3, pgvector
- **Core service**: `wekruit-core-service-cloud-function` — TypeScript, Firebase Cloud Functions v2, Node 20, Firestore
- **VALET user data**: Supabase Postgres with Drizzle ORM — users, resumes, applicationProfiles, preferences (JSONB)
- **Firecrawl**: Self-hosted Docker on Mac Mini (port 3002), 5 containers, handles Workday SPAs
- **Current matching**: FastAPI `POST /match` → pgvector ANN → Python scorer. Being replaced by Cloud Function with filter-first Firestore queries + in-memory cosine sim
- **Handoff doc**: `/Users/wekruitclaw1/Desktop/WeKruit/WEKRUIT-PLATFORM-HANDOFF.md` — full architecture, schemas, algorithms, sync mechanisms

## Constraints

- **VALET untouched**: User sync via Supabase DB Webhooks — no VALET code changes
- **Pipeline stays on Mac Mini**: Heavy batch processing, Firecrawl Docker, daily cron — just adds a sync step
- **Filter before match**: Firestore WHERE clauses first (sponsorship, industry, recency), THEN cosine sim on ~500 docs
- **Collection prefixes**: `platform-`, `matching-`, `outbound-` to match existing repo conventions
- **Cost**: Firestore reads for matching should stay within free tier for early usage; pipeline cost stays ~$0.01/day
- **Existing outbound service**: Must not break existing Cloud Functions in the core service repo

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Keep the console server-rendered | Fastest path to a strong UI without introducing a second frontend architecture | ✓ Good |
| Keep v1.2 separate from the UI milestone | Prevents pipeline expansion from muddying the UI delivery scope | ✓ Good |
| VALET Supabase = user source of truth | Already has rich user data, Drizzle ORM, migrations. Don't duplicate. | v2.0 |
| Supabase DB Webhooks for user sync | Zero VALET code changes, Postgres-level triggers, works with stateless Cloud Functions | v2.0 |
| Firestore for customer-facing reads | Auto-scales, zero maintenance, real-time capable, no connection pooling headaches | v2.0 |
| Filter before match | User requirement. Firestore WHERE → ~500 docs → cosine sim in-memory. Faster, better results. | v2.0 |
| Embeddings in Firestore | 1536 floats = 6KB/doc. 500 docs × 6KB = 3MB. Cosine sim in TS < 50ms. No pgvector on read path. | v2.0 |
| Firecrawl self-hosted | $0 cost, handles Workday SPAs, already running on Mac Mini Docker | v1.2 |
| Open-ended industry/skills | LLM returns diverse values. Hardcoded vocab was too narrow. | v1.2 |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-01 after v2.0 milestone start*
