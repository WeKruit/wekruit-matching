---
gsd_state_version: 1.0
milestone: v2.0
milestone_name: Platform Unification
status: Matching migration live in prod via VALET API; staged Firestore backfill still running
stopped_at: Prod VALET E2E passed for matching, feedback, generate, save, and apply; remaining work is Firestore corpus backfill
last_updated: "2026-04-01T23:05:00-05:00"
progress:
  total_phases: 5
  completed_phases: 5
  total_plans: 5
  completed_plans: 5
---

# Project State

## Project Reference

See: [.planning/PROJECT.md](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/PROJECT.md)

**Core value:** New grads and interns find the best job matches for their skills and preferences through a personalized, filter-first matching engine.
**Current focus:** Finish cutover of the deployed v2.0 matching stack

## Current Position

Phase: 23 of 23
Plan: 23-01
Status: Prod VALET API is cut over to Firebase matching and live E2E passes; staged backfill still in progress
Last activity: 2026-04-01 — prod VALET matching cutover completed, staging/prod `generate` flow verified, and Firestore job corpus reached ~3.7K active docs during staged backfill

Progress: [██████████] 100%

## Performance Metrics

**Previous milestone velocity:**
- v1.2: 5 phases, 5 plans, 1 session
- v1.1: 3 phases, 3 plans, 1 session

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 19 | 1 | docs-only | 1 plan |
| 20 | 1 | user sync | 1 plan |
| 21 | 1 | job sync | 1 plan |
| 22 | 1 | matching | 1 plan |
| 23 | 1 | job board | 1 plan |

## Accumulated Context

### Decisions

- v2.0: Firebase Core Service as central hub — Supabase (users), Postgres (jobs), Firestore (reads)
- v2.0: Supabase DB Webhooks for user sync — zero VALET code changes
- v2.0: Filter-first matching — Firestore WHERE → ~500 docs → cosine sim in TS < 50ms
- v2.0: Embeddings stored in Firestore (1536 floats = 6KB/doc, well within 1MB limit)
- v2.0: Collection prefixes `platform-`, `matching-`, `outbound-` match existing repo conventions
- v2.0: Phase 20 (user sync) and Phase 21 (job sync) are independent after Phase 19
- [Phase 19-handoff-infrastructure-doc]: Matching API runtime on Mac Mini is `start-server.sh` -> uvicorn on `127.0.0.1:8001`, not port 8000
- [Phase 19-handoff-infrastructure-doc]: Daily pipeline launchd log path is `/tmp/matching-daily-update.log`; legacy split cron logs remain `/tmp/wekruit_scraper.log` and `/tmp/wekruit_enrichment.log`
- [Phase 19-handoff-infrastructure-doc]: Firecrawl self-hosted topology is 5 containers (`api`, `playwright-service`, `redis`, `rabbitmq`, `nuq-postgres`) with port `3002` exposed from `api`
- [Phase 19-handoff-infrastructure-doc]: Firestore collections must stay prefix-scoped — `platform-*`, `matching-*`, `outbound-*`; do not create bare `users`/`jobs` collections
- [Phase 20-user-sync-cloud-function]: User sync auth is a shared `X-Webhook-Signature` header secret; Supabase DB Webhooks were modeled around custom headers, not a VALET code change
- [Phase 21-job-sync-pipeline]: `matching-jobs` docs now carry query-ready fields (`jobType`, `locationBuckets`, `searchTokens`, `requiredSkillsIndex`, `salaryMin`, `salaryMax`) so Firebase can own matching and browse reads
- [Phase 22-matching-cloud-function]: Feedback state lives in Firestore collections `matching-feedback` and `matching-saved-jobs`; `applied` is treated as a positive company signal for future ranking
- [Phase 23-job-board-api]: Firestore composite indexes for matching are declared in-repo under `wekruit-core-service-cloud-function/firestore.indexes.json`

### Pending Todos

- Continue the staged production job sync backfill from ~3.7K active Firestore docs toward the full active embedded corpus
- Benchmark production-sized candidate scoring and advanced browse filters against a larger Firestore slice

### Blockers/Concerns

- Firestore composite index limits for complex job board queries — validate during Phase 23
- Bulk load of the full job corpus still needs a live throughput check against Firestore write limits
- Matching latency for ~500 Firestore candidates still needs production benchmarking
- Public Hosting `/api/matching/*` rewrite still is not used, but the live VALET API path is now cut over to Firebase matching

## Session Continuity

Last session: 2026-04-01
Stopped at: Prod VALET API cutover and live E2E are complete — next up continue staged Firestore backfill and measure production-scale performance
Resume file: None
