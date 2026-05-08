# Canonical Tags — Sync Architecture

**Status:** Live (2026-05-08, P7-C unified-canonical-tags)
**Owner:** wekruit-pa (`packages/shared-tags` + `packages/pa-job-tag-enricher`)
**This repo's role:** Informational scrape data only — no canonical mapping.

## Adam directive (2026-05-05)

> "tag must be managed in one place, and our enrichment / human enrichment
> must all flow through this tag, so they share — that's not a match…
> reduce regex judgment."

## Three-repo architecture

```
                         ┌────────────────────────┐
                         │ wekruit-pa             │  ← canonical OWNER
                         │ (this is the truth)    │
                         │                        │
                         │ packages/shared-tags   │  closed enums:
                         │   role-function (17)   │  D1
                         │   industry-sector (42) │  D2 + admin overlay D16
                         │   skills (Skill[])     │  D7 (bucket+baseWeight)
                         │   relevant-tags (open) │  D6 (max 12)
                         │   career-stage (13)    │
                         │   job-type (10)        │
                         │   location (130+)      │
                         │   visa (4)             │  D4
                         │                        │
                         │ packages/              │  LLM-driven canonical
                         │   pa-job-tag-enricher  │  mapping (gpt-5.4-nano
                         │                        │  primary, claude-sonnet-4-6
                         │                        │  + gpt-4.1-mini fallback)
                         │                        │
                         │ apps/functions/        │  ← Firestore trigger
                         │   auto-enrich-...      │  on every matching-jobs
                         │   (paMatchingJobs-     │  doc write, runs canonical
                         │    AutoEnrich)         │  mapping ASYNC, idempotent
                         │                        │  via enricherVersion gate
                         └────────────────────────┘
                                     ↑
                                     │  Firestore write
                                     │
                         ┌────────────────────────┐
                         │ wekruit-core-service   │  ← bridge / sync
                         │ -cloud-function        │
                         │                        │  /api/sync/jobs HTTP
                         │ buildMatchingJobRecord │  receives macmini batch
                         │   passes raw industry  │  + writes Firestore
                         │   etc; does NOT        │  matching-jobs doc
                         │   compute canonical    │
                         │   tags                 │
                         └────────────────────────┘
                                     ↑
                                     │  HTTP batch sync
                                     │  (FIREBASE_SYNC_URL)
                                     │
                         ┌────────────────────────┐
                         │ wekruit-matching       │  ← THIS REPO
                         │ (macmini)              │
                         │                        │
                         │ scrapers (Greenhouse,  │  scrape + role_function
                         │   Lever, Ashby, etc)   │  via title_inference
                         │                        │
                         │ enrichment/classifier  │  free-form `industry`
                         │   .py                  │  + flat `required_skills`
                         │                        │  (informational hints —
                         │                        │  canonical owned upstream)
                         └────────────────────────┘
```

## What macmini owns (this repo)

- **Scraping** (Greenhouse, Lever, Ashby, LinkedIn, Wellfound, Otta, jobright, SimplifyJobs).
- **Title-based `role_function` inference** at scrape time
  (`scraper/title_inference.py::infer_role_function`) — this is a heuristic
  pre-fill; canonical mapping still owned by wekruit-pa enricher.
- **Title-based `seniority_level` inference**
  (`scraper/title_inference.py::infer_seniority`).
- **Free-form `industry` LLM hint** (`enrichment/classifier.py`) — this is
  informational, NOT canonical. Postgres column `jobs.industry` is a free
  string; consumers must NOT assume any closed vocabulary.
- **Flat `required_skills: list[str]`** LLM hint — canonical bucketed
  `Skill[]` is computed by wekruit-pa.
- **`sponsorship: bool | None`** LLM hint — canonical sponsorship is
  combined with v1.7 sponsor-allowlist on the wekruit-pa side.
- **Embeddings** via OpenAI `text-embedding-3-small`.

## What wekruit-pa owns (canonical)

All match-time fields on the Firestore `matching-jobs` doc:

| Field | Type | Source |
|---|---|---|
| `roleFunction` | `string[]` (17 closed enum) | `pa-job-tag-enricher` LLM |
| `industrySector` | `string[]` (42 closed enum + admin overlay) | `pa-job-tag-enricher` LLM |
| `requiredSkills` | `Skill[]` (`{name, bucket, baseWeight, proficiency}`) | `pa-job-tag-enricher` LLM |
| `relevantTags` | `string[]` (open vocab, max 12) | `pa-job-tag-enricher` LLM |
| `seniorityLevel` | `string` (13 enum) | scrape pre-fill OR enricher |
| `locationBuckets` | `string[]` (130+ enum) | enricher OR `getLocationBuckets()` in core-service |
| `jobType` | `string` (10 enum, exact match) | core-service `inferJobType()` from `source_repo` |

The trigger `paMatchingJobsAutoEnrich` (deployed CF) re-enriches on every
matching-jobs write. Idempotency via `enricherVersion` gate (currently
`v1.8.1`) — re-syncs do NOT redo classification unless content changed.

## Decision rationale (2026-05-08, P7-C)

**Three options were on the table:**

| Option | Description | Verdict |
|---|---|---|
| A | Port `packages/shared-tags` TS → Python, keep both in sync | **Rejected.** Adds churn (7 vocab files × frequent edits). Macmini classifier output is overwritten by PA's auto-enrich anyway. |
| B | Macmini calls `paEnrichJobTags` HTTP CF synchronously per job | **Deferred.** Already half-built; adds 6500 extra calls/day for redundant work — `paMatchingJobsAutoEnrich` does it for free via Firestore trigger. |
| C | Emit shared-tags vocab as JSON, sync via build artefact | **Rejected.** Same churn as A; adds CI infrastructure for no canonical-correctness gain. |

**Decision: NONE of A/B/C.** The unified canonical tagging promise is
already kept (100% coverage on 5275 active Firestore matching-jobs as of
2026-05-08, sampling first 8000 active docs). What was missing was clarity:
macmini's stale `INDUSTRY_VOCAB` 38-abbreviation enum was dead code that
violated v1.6 D5 and confused future agents into thinking macmini owned
the vocab.

**P7-C action:** Delete the dead vocab + freeze `industry` as informational
free-form + document the truth here.

## What to do if you need to change the canonical vocab

1. Edit `packages/shared-tags/src/canonical/<vocab>.ts` in wekruit-pa.
2. Bump `ENRICHER_VERSION` in `apps/functions/src/auto-enrich-matching-jobs.ts`.
3. Deploy: `cd apps/functions && pnpm run deploy`.
4. The trigger will re-enrich each matching-jobs doc on next write
   (or run `force-reenrich-stale-jobs.mjs` to bulk-bump).
5. **Do NOT touch this repo for vocab changes.** Macmini classifier output
   is informational and will be overwritten regardless.

## What to do if `paMatchingJobsAutoEnrich` is broken

1. Inspect logs: `gcloud functions logs read paMatchingJobsAutoEnrich --project wekruit-5f89b --region us-central1`.
2. The trigger gracefully fails per-doc — failed enrichments are recoverable
   on next write. No retry loop, no DLQ, no SLO panic — just visibility.
3. If macmini's free-form `industry` string is the only signal available,
   wekruit-pa V16 match falls back to soft scoring on raw string overlap
   (degraded but not broken).

## Anti-patterns to avoid

- ❌ Don't add a closed-vocab enum back to macmini classifier.
- ❌ Don't use `industry` from macmini for hard-filter match — it's a hint.
- ❌ Don't sync TypeScript vocab to Python files in this repo.
- ❌ Don't bypass `paMatchingJobsAutoEnrich` by writing canonical fields
  directly from a backfill script unless you also bump `enricherVersion`
  to a fresh value (otherwise the trigger thinks the doc is enriched).

## Reference

- `wekruit-pa/CLAUDE.md` — v1.6 design lock D1-D16 (16 Adam-locked decisions).
- `wekruit-pa/packages/shared-tags/src/canonical/` — canonical vocab files.
- `wekruit-pa/packages/pa-job-tag-enricher/src/schema.ts` — enricher I/O.
- `wekruit-pa/apps/functions/src/auto-enrich-matching-jobs.ts` — Firestore trigger.
- `wekruit-pa/apps/functions/src/enrich-job-tags-http.ts` — `paEnrichJobTags` HTTP CF.
- `wekruit-core-service-cloud-function/src/services/matching/application/jobSync.ts` —
  `buildMatchingJobRecord` (the bridge between macmini Postgres and Firestore).
