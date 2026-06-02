# Operator runbook — nightly scrape orchestration hardening

This is the WS-C operator companion to `scripts/install-laptop-scrape.sh`,
`scripts/daily-update.sh`, and `scripts/credential-monitor.sh`. It closes the
deploy-hardening gaps GC-4 / AS-2 / CID-02 / CID-04 / CID-05 / IL-6: a SHA-pinned,
fully-migrating, preflight-guarded nightly that **degrades** (skips only sync)
instead of no-op'ing the whole night.

---

## 1. Pin the launchd plist to a fixed repo path (kill sibling-checkout ambiguity)

There are multiple checkouts of this repo on disk (e.g. `~/wekruit-matching` and
`~/Desktop/WeKruit/wekruit-matching`, plus `.worktrees/*`). If the launchd plist
points at the *wrong* one, the nightly silently runs stale code or a half-merged
worktree (this is the GC-4 / AS-2 / CID-02 "working-tree-is-prod" failure class).

**Pin exactly one checkout as the prod runner:**

1. Decide the canonical repo path, e.g. `/Users/<user>/wekruit-matching`. Use the
   real production clone on `main` (or the deploy branch), **not** a worktree
   under `.worktrees/` or `.claude/worktrees/`.
2. Install the agent from *that* directory so the installer bakes the absolute
   path into the generated plist:

   ```bash
   cd /Users/<user>/wekruit-matching        # the canonical checkout
   bash scripts/install-laptop-scrape.sh    # bakes __REPO_ROOT__ = this dir
   bash scripts/install-laptop-scrape.sh --status
   ```

   The installer substitutes `__REPO_ROOT__` / `__USER_HOME__` /
   `__USER__` in `scripts/laptop-fallback/com.wekruit.scrape.daily.plist.template`
   and writes `~/Library/LaunchAgents/com.wekruit.scrape.daily.plist` with
   `WorkingDirectory` + `ProgramArguments` both anchored to the absolute path.

3. Confirm the deployed plist points where you expect (no `__REPO_ROOT__`
   placeholders left, no stray worktree path):

   ```bash
   grep -E 'WorkingDirectory|daily-update.sh' -A1 \
     ~/Library/LaunchAgents/com.wekruit.scrape.daily.plist
   ```

### `.venv` / `PYTHONPATH` pinning

`scripts/daily-update.sh` invokes `.venv/bin/alembic` and `.venv/bin/python`
**relative to the repo root** it `cd`s into. So the venv is automatically pinned
to the same checkout the plist points at — there is nothing extra to set as long
as that checkout has a populated `.venv` (`uv sync --frozen`). Do **not** set a
global `PYTHONPATH` in the plist `EnvironmentVariables`; a stray `PYTHONPATH`
pointing at a sibling checkout would shadow the pinned one. The plist's
`EnvironmentVariables` should carry only `PATH`, `HOME`, and `SCRAPE_LOCK_RUNNER`
(as the template already does).

Verify the venv exists in the pinned checkout:

```bash
ls -la /Users/<user>/wekruit-matching/.venv/bin/python \
       /Users/<user>/wekruit-matching/.venv/bin/alembic
```

---

## 2. SHA-pin / ALLOW_DIRTY contract (what daily-update.sh now enforces)

`scripts/daily-update.sh` refuses to run a dirty working tree as prod (CID-02):

- It captures `RUN_SHA="$(git rev-parse HEAD)"` and echoes
  `runSha=<sha> allowDirty=<0|1>` to stdout + into the pipeline log, and stamps
  `runSha` into the lock-release `--stats-json` for audit.
- If `git status --porcelain` is **non-empty** and `ALLOW_DIRTY` is not `1`, it
  prints the offending changes and **exits 3** — it will not run. Commit or stash
  first, or for a deliberate dev run:

  ```bash
  ALLOW_DIRTY=1 bash scripts/daily-update.sh
  ```

- It does a **best-effort** `git fetch origin` and only **WARNs** if the local
  branch is behind upstream (a laptop runner is frequently offline, so a
  stale-but-clean tree is still a valid prod run — it never aborts on this).

**Exit codes added by the hardening (so a wrapping scheduler can branch):**

| code | meaning |
|------|---------|
| 3 | dirty working tree, `ALLOW_DIRTY` not set — refused |
| 4 | `alembic upgrade head` failed — aborted before lock |
| 5 | preflight HARD FAIL (core dep down) — alert fired, aborted before lock |
| 0 | clean exit (ran, or lock contended and another runner owns today) |

Pre-existing semantics preserved: lock-contended still exits 0; the final exit is
`pipeline.daily`'s exit code.

---

## 3. Migrate-then-run is now consistent everywhere (CID-05)

Previously **only** GitHub Actions ran `alembic upgrade head`; a laptop/macmini
run could execute against a schema older than HEAD. All three entrypoints now
share the migrate-then-run contract:

- `scripts/daily-update.sh` — runs `.venv/bin/alembic upgrade head` after
  sourcing `.env`, **before** the lock acquire; aborts (exit 4) on failure.
- `docker/entrypoint.sh` — already ran `uv run alembic upgrade head` before the
  pipeline (unchanged; a comment now points at this shared contract).
- `.github/workflows/daily-scrape.yml` — already has an `alembic upgrade head`
  step (unchanged).

WS-B additionally wires a `pipeline.daily` startup schema guard
(`ensure_schema_current()`) so any entrypoint that *forgets* to migrate still
fails fast.

---

## 4. Stage-0 preflight + degrade (CID-04 "whole-night-no-op")

Before taking the Firestore lock, `daily-update.sh` runs
`python -m wekruit_matching.pipeline.preflight` (WS-B) and branches on its exit
code:

- **0** — all deps live → proceed.
- **2** — *only* the Firestore/sync credential is down → `export WEKRUIT_SKIP_SYNC=1`
  and continue: **scrape / enrich / embed still run, only the Firestore sync is
  skipped**. `pipeline.daily` honors `WEKRUIT_SKIP_SYNC` (wired by WS-B). This is
  the whole point — a dead sync key no longer wastes the entire night.
- **1** (or any other non-0/2) — a core dependency (DB, etc.) is down → fire the
  post-pipeline webhook with a `failed` / `preflight_hard_fail_*` status and
  **exit 5** before the lock, so we don't burn cost on a doomed run.

---

## 5. Install `credential-monitor.sh` on its own schedule (early page)

`scripts/credential-monitor.sh` runs the **same** preflight a couple of hours
before the nightly and pages (via the existing `post-pipeline-webhook.sh` path)
if a credential is dead — most importantly a dead **Firestore service-account
key** — while there is still time to rotate it before the 6am run.

It takes no lock, writes no data. Exit codes mirror preflight (0 ok / 2 degrade /
1 hard fail). Set `CREDENTIAL_MONITOR_DEGRADE=0` if you only want to be paged on
hard fails, not on the sync-degrade case.

**Example launchd agent** (fires at 08:00 UTC, ~2h before the 10:05 UTC nightly).
Save as `~/Library/LaunchAgents/com.wekruit.credmon.daily.plist`, substituting the
absolute repo path you pinned in section 1:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.wekruit.credmon.daily</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/<user>/wekruit-matching/scripts/credential-monitor.sh</string>
  </array>
  <key>WorkingDirectory</key>
  <string>/Users/<user>/wekruit-matching</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>/Users/<user></string>
  </dict>
  <!-- Local time; 08:00 UTC. Convert to your laptop's TZ the same way
       install-laptop-scrape.sh converts the nightly fire time. -->
  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key><integer>3</integer>
    <key>Minute</key><integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/<user>/Library/Logs/wekruit-credmon.out.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/<user>/Library/Logs/wekruit-credmon.err.log</string>
</dict>
</plist>
```

Install / verify / fire:

```bash
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.wekruit.credmon.daily.plist
launchctl enable "gui/$(id -u)/com.wekruit.credmon.daily"
launchctl kickstart -k "gui/$(id -u)/com.wekruit.credmon.daily"   # fire once now
tail -f ~/Library/Logs/wekruit-credmon.out.log
```

> Alerting requires `PA_MATCHING_WEBHOOK_SECRET` to be present in `.env`
> (same secret `daily-update.sh` uses). Without it the webhook is skipped and the
> monitor logs a warning instead of paging.

---

## 6. Enable the branch-protection "ci" required check (Gate 1/6 — human toggle)

CI being green is only a *gate* if merges are **blocked** when it's red. That is a
GitHub setting a human must toggle — it cannot be enforced from this repo:

1. GitHub → repo → **Settings → Branches → Branch protection rules**.
2. Add/edit the rule for `main` (and the active deploy branch).
3. Enable **"Require status checks to pass before merging"** and select the **`ci`**
   check (the workflow that runs `ruff` + `pytest`).
4. Also enable **"Require branches to be up to date before merging"** so a PR
   can't merge stale and reintroduce a reverted fix.

Until this is on, a red CI run does not stop a merge — re-introducing exactly the
class of regression these gates exist to catch.

---

## 7. FOLLOW-UP — durable Firecrawl env (IL-6) is NOT in this repo's compose

The 2026-05-31 fix patched a **running** `firecrawl-api` container's env
(`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `MODEL_NAME`) but never persisted it to a
compose file, so a container restart re-breaks `/v1/extract` (HTTP 500 from empty
env).

**This repo's `docker-compose.yml` defines only `db` + `app` — there is NO
firecrawl service here**, so WS-C did *not* add firecrawl env keys to it (we do
not invent a service). The `firecrawl-api` container is part of a **separate
self-hosted Firecrawl deployment** (its own `docker-compose.yml`, outside this
repo).

**Action required by the operator (durable IL-6 fix, tracked here as the
follow-up):** in the Firecrawl deployment's own compose, under the
`firecrawl-api` service, add the keys sourced from the host environment (do
**not** hardcode secrets):

```yaml
  firecrawl-api:
    # ...existing config...
    environment:
      OPENAI_API_KEY: ${OPENAI_API_KEY:?set OPENAI_API_KEY in the host env}
      OPENAI_BASE_URL: ${OPENAI_BASE_URL:-https://api.openai.com/v1}
      MODEL_NAME: ${MODEL_NAME:-gpt-5.4-nano}
```

Then `docker compose up -d firecrawl-api` so the values survive restarts. The
matching repo only *calls* Firecrawl over HTTP (`FIRECRAWL_BASE_URL` /
`FIRECRAWL_API_KEY` in `.env`); it does not own the Firecrawl container's
lifecycle.
