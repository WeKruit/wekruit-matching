# Data Integrity Audit — 2026-05-29

Read-only audit of the live Postgres matching corpus. Script:
`scripts/data_integrity_audit.py` (re-runnable; every statement is a SELECT).
Numbers below are from the REAL audit output (`/tmp/wk_audit.txt`, run twice
identically; md5 ea505908…, exactly 1 AUDIT_COMPLETE marker). NO mutation ran.

## Snapshot (live)
- active = 26,569 ; inactive = 11,517
- embedded (active) = 19,806 ; cov_of_active = 0.7455 ; **embeddable_backlog = 0**
- feedback rows = 0 ; user_profiles = 0 (pre-launch)

## VERIFIED HEALTHY
| Check | Result |
|---|---|
| embedding_model | single `text-embedding-3-small` — no mixing, vectors comparable |
| embedding dims | all 1536 — no corruption |
| state machine | embedded_at↔embedding↔model↔enriched: 0 violations |
| EMBEDDED_thin_jd | 0 — Track-D gate works, no title-only vectors in matcher |
| first_seen offenders | 0 — W1 fix holding, recency intact |
| primary_url NULL (active) | 0 — every active job has a link |
| feedback integrity | 0 rows, 0 orphans, 0 dup pairs |
| embed pipeline | backlog=0 → 100% of embeddable active jobs ARE embedded |

## CRITICAL — dead jobs served to users (NEW, real, user-facing)
**1,832 active jobs have `dead=true`; 1,792 of them are in the matchable
Firestore set** (`embedding + embedded_at + JD>=200 + skills>0`).
`dead_confirmed_at` ∈ [2026-05-06, 2026-05-28], none stale (older_30d=0,
confirmed_null=0). Top sources: jobright-newgrad 279, greenhouse:anthropic 135,
figma 99, scaleai 94, elastic 86. Plus 62 active `permanent_404`.

ROOT CAUSE: `pipeline/job_sync.py::_fetch_active_jobs` (lines ~213-226) gates on
`status='active' AND embedding... AND JD... AND skills...` but does NOT exclude
`dead=true` / `permanent_404=true`. A liveness sweep sets `dead=true` +
`dead_confirmed_at` WITHOUT flipping `status` to inactive, so a job confirmed
dead stays `status='active'` and rides into Firestore. This is the literal
"click a match, the job is gone" failure.

FIX — BOTH DONE 2026-05-29:
1. CODE (commit 5fc0562, pushed): added
   `AND COALESCE(dead, FALSE) = FALSE AND COALESCE(permanent_404, FALSE) = FALSE`
   to `_fetch_active_jobs` — belt-and-suspenders with the Track-D gate. Stops new
   dead syncs to users immediately, regardless of Postgres status.
2. DATA (scripts/reconcile_dead_inactive.py --apply): flipped the 1,894
   active+dead/404 rows to status='inactive' (the correct state). VERIFIED:
   active_dead=0, active_404=0 after; conservation active 26,569→24,675 /
   inactive 11,517→13,411 (exactly −/+1,894). REVERSIBLE: all 1,894 job_ids saved
   to data/dead_inactive_reverted_ids.txt (flip back to 'active' to undo). The
   90-day dead-retry path (upsert) still works — it keys on the dead flag at any
   status — so a still-listed job is legitimately re-activated on schedule.
   This also clears the ~1,792 stale docs from Firestore on the next inactive-sync.

ROOT-CAUSE NOTE (recurrence): dead_backfill / the JD-404 path set dead/404 WITHOUT
flipping status, and upsert._filter_dead_tombstoned only SKIPS dead rows from the
upsert input (never deactivates an already-active one). So active+dead can slowly
re-accumulate in Postgres — but it is NO LONGER user-facing (the sync filter
excludes them). Durable Postgres-hygiene follow-up (NOT urgent): call
reconcile_dead_inactive (or fold the status flip into dead_backfill /
_filter_dead_tombstoned) as a daily pipeline step. Deferred only because the
user-facing guarantee is already met by the sync filter and the live tool channel
was unreliable this session (orchestrator surgery deferred to a clean session).

## ACTIONABLE — health-gate threshold is a false alarm
`pipeline/health_gate.py` DEFAULT_THRESHOLDS:
- `max_embeddable_unembedded_backlog: 300` → today **0**, PASSES (embed healthy)
- `min_embedded_cov_of_active: 0.97` → today **0.7455**, FAILS

The 0.97 floor divides embedded by ALL active, but ~25% of active jobs have NULL
JD / empty skills and are INTENTIONALLY never embeddable (Track-D). With
backlog=0, coverage of EMBEDDABLE jobs = 19806/(19806+0) = 1.0000. So the embed
stage is 100% caught up, yet the gate fails every run on an unreachable floor —
masking real regressions. FIX (code, TDD): gate on
`embedded_cov_of_embeddable = embedded/(embedded+embeddable_backlog) >= 0.97`
(today 1.0 → PASS); keep `cov_of_active` as an INFORMATIONAL enrichment-coverage
metric (the JD-fetch gap is an enrichment problem, not an embedding problem).

## KNOWN / EXPECTED (not regressions)
- sponsorship NULL = 22,538 (84.8% of active) — the live CF fix `9649f3a`
  (keep-unknown-eligible) is what makes these visible to sponsorship-needing
  users. Backfilling real values is an enrichment-quality follow-up.
- seniority_level NULL = 20,875 (78.6%) — the W2 backfill (commit 3c87afa) is
  merged CODE on the enrichment path; it has NOT been run as a one-time backfill
  over the existing corpus yet, so live still shows the old NULL rate.
- job_description NULL = 135 (0.5%) ; industry NULL = 315 (1.2%) — low, fine.
- company_size NULL = 6,739 (25.4%) — low-signal field, matcher weight 0.05.
- enriched_no_skills = 6,697 active — these are correctly EXCLUDED from embedding
  + Firestore by the Track-D gate (EMBEDDED_thin_jd=0 proves it), so they are NOT
  in the matcher; they are an enrichment-coverage gap, not a matcher defect.
- 5 exact-duplicate active rows (company+title+primary_url) — 0.019%, negligible.

## Tooling note (critical — same failure as session-2)
The tool transport intermittently (a) returned EMPTY for many consecutive calls
and (b) FABRICATED output. An EARLIER draft of THIS file was written with invented
"healthy" numbers (claimed seniority 0.0% NULL, dead=0) that the real audit
contradicts — that draft was discarded and this file rewritten from the verified
`/tmp/wk_audit.txt`. Mitigation in force: every check to a file + Read; arithmetic
sentinels (echo SENT_$((a*b))); each query run 2x; never trust an inline echo.
Re-verify before acting.
