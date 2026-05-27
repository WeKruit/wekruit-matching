# Scrape pipeline fault tolerance — design + operator guide

> 2026-05-22..27 outage: the macmini went unreachable for five days and the
> scrape pipeline silently stopped feeding `matching-jobs`. Match quality
> degraded because nothing else could trigger the daily run. This document
> describes how that is no longer possible.

## Architecture

Three independent triggers can fire the same daily pipeline. A Firestore
distributed lock guarantees only one of them actually writes Firestore each
day.

```
                        ┌───────────────────────────┐
                        │  Firestore                 │
                        │  pa-system-locks/          │
                        │    scrape-daily-YYYY-MM-DD │
                        └────────────▲──────────────┘
                                     │ atomic create()
            ┌────────────────────────┼────────────────────────┐
            │                        │                        │
    ┌───────┴────────┐      ┌────────┴───────┐      ┌─────────┴────────┐
    │ macmini        │      │ GH Actions     │      │ Adam laptop      │
    │ launchd 06 ET  │      │ cron 10 UTC    │      │ ad-hoc `make     │
    │ daily-update.sh│      │ daily-scrape   │      │ scrape-once`     │
    └───────┬────────┘      └────────┬───────┘      └─────────┬────────┘
            │                        │                        │
            └────────── First runner to `doc.create()` wins ──┘
                                     │
                            pipeline.daily writes
                                     │
                                     ▼
                            matching-jobs (Firestore)
```

Outcome:
- Mac mini offline → GH Actions still runs today's scrape.
- GH Actions outage → Mac mini still runs.
- Both run → second one sees `LockState.CONTENDED`, exits quietly, no double-write.
- Yesterday's runner crashed mid-pipeline → today's lock starts clean (date-keyed).
- Today's runner crashes without releasing → stale-lock recovery kicks in after 4h.

## Components

### `src/wekruit_matching/lock.py`

Pure-Python module + CLI. Public API:

```python
with DailyScrapeLock(acquired_by="github-actions:run-12345") as lock:
    if lock.state is LockState.CONTENDED:
        sys.exit(0)
    if lock.state is LockState.ALREADY_RUN:
        sys.exit(0)
    # ... run pipeline ...
    lock.mark_outcome("success", stats={"jobsNew": 1234})
```

CLI for shell wrappers:

```bash
# acquire — exit 0 = go, exit 2 = quiet skip
python -m wekruit_matching.lock acquire --acquired-by "$HOSTNAME"

# release — always safe (tolerant of double-release)
python -m wekruit_matching.lock release --outcome success --stats-json '{"jobsNew":1234}'
```

Tunables in `lock.py`:

| Constant | Default | Purpose |
|---|---|---|
| `LOCK_COLLECTION` | `pa-system-locks` | Firestore collection name |
| `LOCK_KEY_PREFIX` | `scrape-daily-` | Doc-ID prefix (date suffix is UTC) |
| `STALE_AFTER_SECONDS` | `14400` (4h) | Lock older than this can be stolen |

### `.github/workflows/daily-scrape.yml`

Runs at `cron: "0 10 * * *"` (10:00 UTC = 06:00 ET, matches the macmini's
historical cadence). Steps:

1. Spin up `pgvector/pgvector:pg16` as a service container.
2. `uv sync --frozen` + `alembic upgrade head`.
3. Materialize `FIREBASE_SERVICE_ACCOUNT_JSON` to a file referenced by
   `GOOGLE_APPLICATION_CREDENTIALS` (file is mode 0600, not env).
4. Acquire today's lock. Skip the pipeline step if contended/already-run.
5. Run `pipeline.daily`, tee log to artifact.
6. Release the lock with `outcome` parsed from the pipeline's
   `pipelineStatus=` sentinel line. Upload the log as an artifact (14-day
   retention).

Required Action secrets (paste in **Settings → Secrets and variables → Actions**):

```
FIREBASE_SERVICE_ACCOUNT_JSON   (paste the full JSON on one line)
ANTHROPIC_API_KEY
OPENAI_API_KEY
SILICONFLOW_API_KEY
GITHUB_TOKEN_SCRAPER            (PAT with public_repo — NOT the default GITHUB_TOKEN)
FIRECRAWL_API_KEY               (optional)
SERPER_API_KEY                  (optional)
MAILGUN_API_KEY                 (optional)
FIREBASE_SYNC_API_KEY
```

Manual trigger:

```bash
gh workflow run daily-scrape.yml -F reason="recovery after macmini outage"
```

### `scripts/daily-update.sh` (macmini launchd)

Two changes from the historical version:

1. Resolves the repo root from `$BASH_SOURCE` so the script works under
   any user account (originally hardcoded to `/Users/wekruitclaw1/...`).
2. Calls `lock acquire` before the pipeline and `lock release` after the
   webhook. Exits 0 on contention so the launchd job doesn't retry.

The Adam laptop fallback (`make scrape-once` from the Docker quick-start)
exercises the same lock — set `SCRAPE_LOCK_RUNNER="adam-laptop-fallback"`
in the env for clear attribution in the audit doc.

## Verifying the system

```bash
# 1. Did today's lock get acquired?
gcloud firestore documents describe \
  "pa-system-locks/scrape-daily-$(date -u +%Y-%m-%d)" \
  --project wekruit-5f89b

# 2. Did GitHub Actions run today?
gh run list --workflow daily-scrape.yml \
  --created ">=$(date -u +%Y-%m-%d)" \
  --status success --limit 1

# 3. Did matching-jobs actually advance?
node /Users/adam/Desktop/WeKruit/wekruit-pa/scripts/scrape-health.mjs
```

A passing day shows: one lock doc with `outcome: "success"`, one GH Actions
run with `conclusion: success`, and newest `matching-jobs.syncedAt` <24h.

## Operator runbook

| Symptom | Likely cause | Recovery |
|---|---|---|
| GH Actions skipped with `LockState.CONTENDED` | Mac mini grabbed today's lock first — expected after macmini recovers from an outage | None. Confirm by reading the lock doc's `acquiredBy`. |
| GH Actions ran but no Slack alert and `paScrapeFreshnessMonitorDaily` still says stale | Stage 4 sync failed (auth/rate-limit) | Inspect the Action's `pipeline-log` artifact for `firebaseSync: ...` line. Re-run with `gh workflow run daily-scrape.yml -F reason=resync`. The lock CLI will see `outcome:"failed"` and allow the re-run. |
| Lock doc exists but `acquiredBy` is gibberish | Someone fired the workflow manually with an unusual `SCRAPE_LOCK_RUNNER` | None — informational only. |
| Two consecutive days show `outcome: "failed"` | Upstream provider (SimplifyJobs / ATS) is degraded | Open an incident issue, check Anthropic/OpenAI status pages. |

## Phase B follow-up (separate PR)

The Postgres state inside the container is rebuildable but losing it forces
a cold-start re-enrich (~$50). Migrating `dedup_skipped_signatures` and
`dead_url_tombstone` to Firestore makes the pipeline truly stateless:
losing any single runner's PG volume becomes a non-event. Tracked at the
top of `.planning/` (forthcoming `INITIATIVE-firestore-dedupe-store.md`).
