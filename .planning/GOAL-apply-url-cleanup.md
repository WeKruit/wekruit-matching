# GOAL — Apply-URL cleanup (kill jobright redirect in match payload)

**Status**: spec / not started
**Locked**: 2026-05-18 by Adam
**Triggering observation**: 78% of active jobright source jobs still emit `jobright.ai/jobs/info/<hex>` as primary_url. Serper backfill firing hourly but 0 resolutions (allowlist filter rejects every valid hit). Match payload reaches candidates with jobright tracker URL.

Adam's directive verbatim:
> "filter是干什么？我们filter要干嘛？？" → kill the filter
> "Q1 不要reject，全部不要block" → no allowlist, no blocklist
> "只要不是jobright就行" → only jobright is excluded
> "你前两天说green check it's fake" → tighten green definition

---

## ENVIRONMENT — read this if you're cold-starting

### Repos involved

| Repo | Path | Purpose |
|---|---|---|
| `wekruit-matching` (Python, macmini) | `/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching` | Postgres scrapers + Stage 2 enrichment |
| `wekruit-pa` (TS, monorepo) | `/Users/adam/Desktop/WeKruit/wekruit-pa` | Firestore CFs incl. `paBackfillAtsUrlsBatch`, dashboard, match engine |

### SSH

`~/.ssh/config` host alias: **`wekruit-mini`** → `100.83.121.89` user `wekruitclaw1`.

```bash
ssh wekruit-mini "ls /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching"
```

### GitHub push from macmini

```bash
ssh wekruit-mini "cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && \
  TOKEN=\$(grep '^GITHUB_TOKEN=' .env | cut -d= -f2) && \
  git remote set-url origin \"https://x-access-token:\$TOKEN@github.com/WeKruit/wekruit-matching.git\" && \
  git push origin main && \
  git remote set-url origin https://github.com/WeKruit/wekruit-matching.git"
```

### Macmini key files

| Path | Purpose |
|---|---|
| `.venv/bin/python` | Pipeline interpreter |
| `src/wekruit_matching/scraper/*_direct.py` | greenhouse / lever / ashby / wellfound / linkedin / otta scrapers — emit `Job(primary_url=<real ATS URL>)` |
| `src/wekruit_matching/scraper/jobright.py`, `jobright_github.py` | jobright scrapers — emit `Job(primary_url='https://jobright.ai/jobs/info/...')` (redirect) |
| `src/wekruit_matching/pipeline/job_sync.py` | sync to Firestore via HTTP CF |
| `/Users/Shared/wekruit/.env-secrets` | runtime env vars (`JOBRIGHT_USE_GIT_DELTA=1` already on) |
| `/Users/Shared/wekruit/run-pipeline.sh` | daily launchd entry, 06:00 local |

### wekruit-pa key files

| Path | Purpose |
|---|---|
| `apps/functions/src/backfill-ats-urls.ts` | Pure resolver `resolveAtsUrl` + `createSerperSearch` + `isAtsHost` (← to delete) |
| `apps/functions/src/backfill-ats-urls-batch.ts` | `paBackfillAtsUrlsBatch` onSchedule wrapper (hourly cron `0 * * * *`) |
| `apps/job-rec/src/tools/query-matching-jobs-v16.ts` | V16 match engine — currently reads `atsApplyUrl` then `primaryUrl`. Need confirm match payload to candidate uses `atsApplyUrl` first |
| `apps/functions/src/index.ts` | CF barrel export |

### Firebase

- Project: `wekruit-5f89b`
- Deploy: `cd apps/functions && pnpm run deploy` (auto-runs build + smoke + typecheck + tests)
- Secrets: `SERPER_API_KEY` lives in Firebase secrets; `gcloud secrets versions access latest --secret=SERPER_API_KEY --project=wekruit-5f89b`

### Verify-by-doing one-liners

```bash
# Live Firestore matching-jobs count
node -e "
  const fs=require('fs'),os=require('os'),path=require('path')
  const j=JSON.parse(fs.readFileSync(path.join(os.homedir(),'.config/configstore/firebase-tools.json'),'utf8'))
  fetch('https://firestore.googleapis.com/v1/projects/wekruit-5f89b/databases/(default)/documents:runAggregationQuery',{
    method:'POST',headers:{Authorization:'Bearer '+j.tokens.access_token,'Content-Type':'application/json'},
    body:JSON.stringify({structuredAggregationQuery:{structuredQuery:{from:[{collectionId:'matching-jobs'}]},aggregations:[{alias:'n',count:{}}]}}),
  }).then(r=>r.json()).then(d=>console.log('count:',d[0]?.result?.aggregateFields?.n?.integerValue))
"

# PG ats coverage
ssh wekruit-mini "cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && .venv/bin/python -c \"
from wekruit_matching.config import get_settings
from wekruit_matching.db.connection import _sqlalchemy_url_to_libpq
import psycopg
with psycopg.connect(_sqlalchemy_url_to_libpq(get_settings().database_url), autocommit=True) as c, c.cursor() as cur:
    cur.execute(\\\"SELECT count(*) FROM jobs WHERE status='active'\\\")
    print('active:', cur.fetchone()[0])
    cur.execute(\\\"SELECT count(*) FROM jobs WHERE status='active' AND ats_apply_url IS NOT NULL\\\")
    print('active+ats_url:', cur.fetchone()[0])
    cur.execute(\\\"SELECT count(*) FROM jobs WHERE status='active' AND primary_url LIKE '%jobright.ai%'\\\")
    print('active+jobright_in_primary:', cur.fetchone()[0])
\""

# Serper backfill last 5 runs
gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name="pabackfillatsurlsbatch" AND jsonPayload.message=~"backfill_batch_complete"' \
  --project=wekruit-5f89b --limit=5 --freshness=1d --format=json 2>/dev/null | \
  python3 -c "import json,sys; [print(e['timestamp'], e.get('jsonPayload',{})) for e in json.load(sys.stdin)]"
```

---

## LIVE EVIDENCE (2026-05-18)

| Source | Active | resolved (ats_apply_url) | jobright in primary_url |
|---|---|---|---|
| jobright-newgrad | 13,858 | 3,042 (22%) | 13,858 (100%) |
| jobright-intern | 1,939 | 62 (3%) | 1,939 (100%) |
| greenhouse:stripe | 184 | 0 | 0 |
| ashby:openai | 193 | 0 | 0 |
| greenhouse:anthropic | 182 | 1 | 0 |

Backfill CF firing hourly; `missCount=200 / 200 every run / serperCalls=200 / pass1=0 pass3=0` → 100% miss → ~$144/mo burn.

Live Serper test for `"Software Engineer" "anthropic" careers apply` returns top-1 = `anthropic.com/careers/jobs` (perfect). Current allowlist (`ATS_HOSTS = greenhouse.io,lever.co,ashbyhq.com,myworkdayjobs.com,bamboohr.com,teamtailor.com`) rejects it.

---

## DECIDED DESIGN (do not re-litigate)

1. **No allowlist**. Delete `ATS_HOSTS` + `isAtsHost`.
2. **No blocklist**. Adam directive — manual lists never maintained correctly.
3. **One exclusion only**: hostname includes `"jobright"` → skip. Everything else passes (aggregators OK, ATS OK, careers pages OK).
4. **Picker = top-1 non-jobright** from Serper organic hits.
5. **Direct ATS scrapers** (greenhouse/lever/ashby/wellfound/linkedin/otta): emit `Job(ats_apply_url=primary_url)` at INSERT time when `primary_url` is non-jobright. Zero Serper cost for these.
6. **green** definition: end-to-end means candidate-facing apply URL contains no `jobright.ai` AND Serper backfill resolution rate >= 60% AND no traceback. `pipelineStatus=success` alone is NOT green.

---

## DELIVERABLES

### Phase 1 — macmini direct-ATS scrapers (Python)

Files (set `ats_apply_url = primary_url` if `primary_url` does not contain `jobright`):

- `src/wekruit_matching/scraper/greenhouse_direct.py` line ~238
- `src/wekruit_matching/scraper/lever_direct.py` line ~194
- `src/wekruit_matching/scraper/ashby_direct.py` line ~233
- `src/wekruit_matching/scraper/wellfound.py` line ~230
- `src/wekruit_matching/scraper/linkedin.py` line ~344
- `src/wekruit_matching/scraper/otta.py` line ~150

Pattern (each file):

```python
job_id = generate_job_id(...)  # existing line
content_hash = compute_content_hash(...)  # existing line
ats_apply_url = apply_url if apply_url and "jobright" not in apply_url else None

return Job(
    job_id=job_id,
    ...
    primary_url=apply_url or None,
    ats_apply_url=ats_apply_url,
    ...
)
```

Verify `Job` model accepts `ats_apply_url` kwarg (`src/wekruit_matching/models/job.py`). If not, add field.

Tests: add one case per scraper to its existing test file (`tests/test_scraper_*.py`) asserting `ats_apply_url` populated when `apply_url` is non-jobright.

### Phase 2 — wekruit-pa Serper picker rewrite

File: `apps/functions/src/backfill-ats-urls.ts`

Replace the entire allowlist apparatus:

```ts
// DELETE:
export const ATS_HOSTS = [...]
export function isAtsHost(url) { ... }

// REPLACE resolveAtsUrl:
export async function resolveAtsUrl(job, deps): Promise<ResolveOutcome> {
  // Pass 1 — primaryUrl already non-jobright? Use it directly.
  if (job.primaryUrl) {
    const host = safeHostname(job.primaryUrl)
    if (host && !host.includes("jobright")) {
      return { kind: "pass1", url: job.primaryUrl }
    }
  }
  // Pass 3 — Serper search. Need title + company for a meaningful query.
  const title = (job.jobTitle ?? job.roleTitle ?? "").trim()
  const company = (job.companyName ?? "").trim()
  if (!title || !company) return { kind: "miss" }
  const url = await deps.serper(title, company)
  return url ? { kind: "pass3", url } : { kind: "miss" }
}

// REPLACE createSerperSearch internal filter:
return async (title, company) => {
  const queries = [
    `"${title}" "${company}" careers apply`,
    `"${title}" "${company}"`,
  ]
  for (const q of queries) {
    const data = await fetchSerper(q)  // existing code path
    for (const hit of data?.organic ?? []) {
      const link = hit.link
      if (!link) continue
      const host = safeHostname(link)
      if (host && host.includes("jobright")) continue   // ONLY exclusion
      return link
    }
  }
  return null
}

function safeHostname(url: string): string | null {
  try { return new URL(url).hostname.toLowerCase() } catch { return null }
}
```

Tests: `apps/functions/src/__tests__/backfill-ats-urls.test.ts` (or co-located). Update:
- `isAtsHost` deletion → test deletion
- Add Serper fixture: top-1 = `anthropic.com/careers/jobs` → expect pass3 returns that URL
- Add fixture: hits = `[jobright.ai/x, anthropic.com/careers]` → expect skip jobright, return anthropic
- All existing 130+ tests must remain green (touch the test file as needed for the deletion).

### Phase 3 — re-trigger backfill to clear backlog

- `apps/functions/src/backfill-ats-urls-batch.ts` already exists with hourly schedule. After deploy, the next hourly fire will run with new picker.
- Backlog: 19,612 active jobs with `ats_apply_url IS NULL`. At 200/hour = ~4 days to clear naturally.
- **Accelerate**: invoke `paBackfillMatchingJobsAtsUrl` callable in a loop until queue empty. Pseudo:
  ```bash
  for i in $(seq 1 100); do
    firebase functions:call paBackfillMatchingJobsAtsUrl --project wekruit-5f89b --data '{"batchSize":200}' 2>&1 | tail -1
    sleep 5
  done
  ```
  Or write `scripts/drain-ats-backfill.mjs` that loops via REST until `eligibleCount=0`.

### Phase 4 — match payload composeApplyUrl

File: `apps/job-rec/src/tools/query-matching-jobs-v16.ts`

Confirm match output picks `ats_apply_url` first, falls back to `primary_url` only when ats null. Search for places where the match output object writes a URL field; ensure precedence is `ats_apply_url → primary_url`. If `primary_url` is the only thing returned, fix.

Add unit test: a job with `ats_apply_url=X, primary_url=Y` → match output URL = X.

### Phase 5 — drop Firecrawl-fallback dead path (optional)

If Phase 2 + 3 land coverage ≥60%, the `pa-ats-resolve-priority` queue retry loop is still useful but the misCount stamp `urlResolutionAttemptedAt` becomes meaningful again. Leave as-is.

---

## DONE CRITERIA — verify-by-doing

| # | Check | Command | Pass condition |
|---|---|---|---|
| 1 | Direct ATS scrapers populate ats_apply_url at insert | `ssh wekruit-mini` + run a single-source scrape (Python REPL); `print(jobs[0].ats_apply_url)` | non-null, equals primary_url |
| 2 | macmini scraper tests green | `ssh wekruit-mini "cd ... && .venv/bin/python -m pytest tests/test_scraper_*.py"` | All pass |
| 3 | Serper picker accepts any non-jobright | `pnpm --filter @pa/functions test backfill-ats-urls` | Updated cases green |
| 4 | Deploy succeeds | `cd apps/functions && pnpm run deploy` | predeploy gate passes |
| 5 | Next hourly fire of `paBackfillAtsUrlsBatch` resolves > 0 | `gcloud logging read ...pabackfillatsurlsbatch...backfill_batch_complete` (latest run) | `pass1Count + pass3Count > 100` (out of 200) |
| 6 | Backlog drained | PG query `SELECT count(*) FROM jobs WHERE status='active' AND ats_apply_url IS NULL` | < 5,000 |
| 7 | Match payload contains no jobright | Spot-check 5 random rec outputs via `/admin/match-debug` | Zero URLs contain `jobright.ai` |
| 8 | Live scenario | `node tests/scenarios/runner.mjs scenarios/full-match-with-ats.yaml` | candidate-facing message has clean URLs |

---

## DEPLOY ORDER

1. Phase 1 commits on macmini → push to GH (use token-in-url pattern above).
2. Next launchd run (06:00 local) picks up scraper changes — OR trigger manually `ssh wekruit-mini "nohup /Users/Shared/wekruit/run-pipeline.sh > /tmp/wekruit-manual-run-\$(date -u +%Y%m%d-%H%M%S).log 2>&1 &"`.
3. Phase 2 + 4 in wekruit-pa → `cd apps/functions && pnpm run deploy`.
4. Phase 3 drain script — run after Phase 2 deploy.
5. Verify all 8 criteria. Paste evidence. Only then mark green.

---

## EXECUTION STYLE

- Same-error-twice = STOP, switch approach.
- All tests green or fix root cause. No `--no-verify`.
- Predeploy gate fails → debug. Don't bypass.
- Paste actual output for each criterion. "ok" is not evidence.
- Owner mindset: any unrelated bug found → spawn-task or fix inline.

## NON-GOALS

- Don't rebuild Stage 2 Firecrawl pipeline.
- Don't add LLM as fallback resolver (premature).
- Don't change daily launchd schedule.
- Don't touch v1.6 canonical-tag pipeline.
- Don't add company-tier scoring (separate GOAL doc).

---

## SYNC GAP — INCLUDED IN GOAL (added 2026-05-18, Adam directive)

After initial ship Adam asked *"but we have 10k+ documents no?"*. Audit confirmed
PG had **17,748 active+embedded** jobs but Firestore only held **3,662 active**.
The ~14k gap was caused by the PG→Firestore sync silently failing for ~3 days.

### Root cause

1. **Receiver-side TX too big** —
   `wekruit-core-service-cloud-function/src/services/matching/repositories/matchingJobRepository.ts`
   chunked incoming HTTP batches into Firestore `writeBatch` of up to **500 docs**.
   Each `matching-jobs` doc carries a 1536-float embedding (~24 KB) + job_description
   blob (~50–100 KB total). 500-doc commit blew past Firestore's 10 MiB TX cap.
   `matching-api` returned `500 INVALID_ARGUMENT: Transaction too big`.

2. **Client retry-storm** — macmini `pipeline/job_sync.py:_post_jobs_batch`
   recursively halves on 500 response (200 → 100+100 → 50+50 → …). 5-minute sync
   turned into hour-long retry storm. Daily launchd didn't drain successfully for
   ~3 days. Firestore active stayed stuck at 3,662 while PG kept growing.

### Cross-repo fix (locked)

| Repo | File | Change | Commit |
|---|---|---|---|
| wekruit-core-service-cloud-function | `src/services/matching/repositories/matchingJobRepository.ts` | `FIRESTORE_BATCH_LIMIT` 500 → 25 | PR #6 merged to main |
| wekruit-matching (macmini) | `src/wekruit_matching/config.py` | `firebase_sync_batch_size` default 200 → 50 | `965e884` on origin/main |

The two caps are interlocked: 50 client docs/request × ~100 KB/doc = ~5 MiB request,
which the receiver splits into 2 internal Firestore TXs of 25 docs ≈ 2.5 MiB each.
Well under the 10 MiB cap from both directions. Removes the retry-split storm too.

### Sync gap done criteria

| # | Check | Command | Pass condition |
|---|---|---|---|
| S1 | Receiver patched + deployed | `gcloud functions describe matching-api --region=us-central1 --project=wekruit-5f89b --gen2` | revision deployed post-2026-05-18T20:00 UTC |
| S2 | Macmini client patched | `ssh wekruit-mini "grep firebase_sync_batch_size .../config.py"` | shows `Field(50)` |
| S3 | Full sync runs without TX-too-big | `ssh wekruit-mini "grep -c 'Transaction too big' /tmp/full-sync-*.log"` | 0 in any post-fix log |
| S4 | Firestore active matches PG active+embedded | `node /tmp/agg-active.mjs` vs PG count | within ±100 docs |
| S5 | Daily launchd run finishes without retry-split warnings | `grep -c "Firebase sync batch failed at" /tmp/wekruit-manual-run-*.log` | 0 after launchd run starting post-fix |

### Followup (parked for separate goal)

- Emit a daily health metric: Firestore active vs PG active+embedded delta. Alert if > 500.
- Monitor macmini launchd sync logs for retry-split warnings — should stay zero permanently.

---

## REFERENCES

- v1.7 backfill phase: see git log around `apps/functions/src/backfill-ats-urls.ts` initial commit (~mid May).
- Earlier ship doc: `.planning/GOAL-company-tier-yc-match.md` in wekruit-pa (separate milestone).
- v2 stable job_id work: macmini commits `deb7323..1d1d048`.
- Cross-repo sync fix: wekruit-core-service-cloud-function PR #6 + wekruit-matching `965e884`.

**START.**
