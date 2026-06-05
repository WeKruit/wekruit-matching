#!/bin/bash
# Daily job pipeline: scrape, enrich, embed + email notifications
# Runs via launchd at 06:00 local time on the laptop (StartCalendarInterval in
# ~/Library/LaunchAgents/com.wekruit.scrape.daily.plist; currently EDT => 10:00
# UTC). Re-aligned 2026-06-03 from a drifted 10:05 local. Adjust the plist Hour
# (local time) + `launchctl unload/load` it to change the fire time.
# v1.5 Stream-A2: post-pipeline webhook to wekruit-pa.

# Resolve the repo root from this script's location so this works on any
# user account (originally hardcoded to /Users/wekruitclaw1). Adam's laptop
# launchd fallback (scripts/install-laptop-scrape.sh) + the Docker image
# both lean on this. The script lives in `<repo>/scripts/` so the parent
# of $SCRIPT_DIR is the repo root.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

# ---------------------------------------------------------------------------
# SHA-pin guard (CID-02 "working-tree-is-prod" fix).
# ---------------------------------------------------------------------------
# The nightly run must execute a known, committed revision — never a dirty
# checkout that happens to be sitting on the laptop. Capture the SHA we are
# about to run, and refuse to proceed if a TRACKED file has uncommitted
# changes (unless an operator explicitly opts in with ALLOW_DIRTY=1 for dev).
# A best-effort `git fetch` lets us WARN (not fail — the laptop may be
# offline) if the local branch is behind upstream.
RUN_SHA="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
ALLOW_DIRTY="${ALLOW_DIRTY:-0}"

# -uno: only TRACKED modifications count as "dirty". The laptop checkout always
# carries untracked dev artifacts (.planning/, .claude/, .worktrees/) that must
# NOT block prod — otherwise every nightly aborts and operators just pin
# ALLOW_DIRTY=1, defeating the guard. Uncommitted edits to committed files are
# the real "half-finished code" signal we refuse on.
DIRTY="$(git status --porcelain -uno 2>/dev/null)"
if [[ -n "$DIRTY" && "$ALLOW_DIRTY" != "1" ]]; then
  echo "[daily-update] ERROR: tracked working tree is dirty — refusing to run as prod." >&2
  echo "[daily-update] uncommitted changes to tracked files:" >&2
  echo "$DIRTY" | sed 's/^/[daily-update]   /' >&2
  echo "[daily-update] commit/stash the changes, or set ALLOW_DIRTY=1 to override (dev only)." >&2
  exit 3
fi

# Best-effort freshness check. Never abort on this — a laptop runner is
# frequently offline and a stale-but-clean tree is still a valid prod run.
if git fetch origin >/dev/null 2>&1; then
  UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || echo '')"
  if [[ -n "$UPSTREAM" ]]; then
    BEHIND="$(git rev-list --count "HEAD..${UPSTREAM}" 2>/dev/null || echo 0)"
    if [[ "$BEHIND" =~ ^[0-9]+$ && "$BEHIND" -gt 0 ]]; then
      echo "[daily-update] WARN: local HEAD is ${BEHIND} commit(s) behind ${UPSTREAM} — running anyway." >&2
    fi
  fi
else
  echo "[daily-update] note: git fetch failed (offline?) — skipping behind-upstream check." >&2
fi

echo "[daily-update] runSha=${RUN_SHA} allowDirty=${ALLOW_DIRTY}"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PIPELINE_LOG="/tmp/wekruit-matching-daily-$(date -u +%Y%m%d-%H%M%S).log"

# Capture RUN_SHA into the pipeline log up front so the committed revision is
# always recorded next to the run's output (the post-pipeline webhook arg slots
# are fixed — arg 11 is runId — so we surface the SHA here + on lock release).
echo "[daily-update] runSha=${RUN_SHA} startedAt=${STARTED}" | tee -a "$PIPELINE_LOG"

# ---------------------------------------------------------------------------
# Migrate + schema assert (CID-05 fix — previously ONLY GitHub Actions ran
# `alembic upgrade head`, so a laptop/macmini run could execute against a
# schema older than HEAD). Mirror the .venv/bin convention used below and the
# entrypoint.sh / daily-scrape.yml migrate-then-run contract.
# ---------------------------------------------------------------------------
echo "[daily-update] running alembic upgrade head" | tee -a "$PIPELINE_LOG"
if ! .venv/bin/alembic upgrade head 2>&1 | tee -a "$PIPELINE_LOG"; then
  echo "[daily-update] ERROR: alembic upgrade head failed — aborting before lock acquire." >&2
  exit 4
fi

# ---------------------------------------------------------------------------
# Stage 0 preflight (CID-04 "whole-night-no-op" fix). Probe external deps
# BEFORE taking the lock so a dead credential is known without burning today's
# lock slot. WS-B's `pipeline.preflight` exit codes:
#   0 = all deps live                          -> proceed
#   2 = ONLY Firestore/sync credential is down -> degrade: skip sync, keep
#       scrape/enrich/embed (export WEKRUIT_SKIP_SYNC=1, pipeline.daily reads it)
#   1 = hard fail (DB/core dep down)           -> abort + alert
# ---------------------------------------------------------------------------
echo "[daily-update] running preflight (pipeline.preflight)" | tee -a "$PIPELINE_LOG"
# This script does not run under `set -e`, so no errexit guard is needed here;
# ${PIPESTATUS[0]} captures the preflight (not tee) exit code regardless.
.venv/bin/python -m wekruit_matching.pipeline.preflight 2>&1 | tee -a "$PIPELINE_LOG"
PREFLIGHT_RC="${PIPESTATUS[0]}"
case "$PREFLIGHT_RC" in
  0)
    echo "[daily-update] preflight OK — all deps live." | tee -a "$PIPELINE_LOG"
    ;;
  2)
    export WEKRUIT_SKIP_SYNC=1
    echo "[daily-update] preflight DEGRADE: Firestore/sync credential down — exporting WEKRUIT_SKIP_SYNC=1 (scrape/enrich/embed still run, sync skipped)." | tee -a "$PIPELINE_LOG"
    ;;
  *)
    # Treat exit 1 (and any other non-0/2 code) as a hard fail: a core dep is
    # down, so running the pipeline would only burn cost. Fire the webhook with
    # a failed/preflight_hard_fail status, then abort BEFORE the lock acquire.
    echo "[daily-update] preflight HARD FAIL (rc=${PREFLIGHT_RC}) — aborting before lock acquire." >&2
    PF_NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    scripts/post-pipeline-webhook.sh \
      "failed" \
      "$STARTED" \
      "$PF_NOW" \
      "0" "0" "0" "0" "0" \
      "SimplifyJobs" \
      "preflight_hard_fail_rc_${PREFLIGHT_RC}" \
      || true
    exit 5
    ;;
esac

# ---------------------------------------------------------------------------
# Firecrawl health-gate (2026-06-05). The self-hosted Firecrawl (localhost:3002)
# is a SPOF: on 2026-06-05 a wedged firecrawl-api container held a single render
# ~1.5h and Stage 1.7 ran ~4h49m. A container that wedges OVERNIGHT would do it
# again. Probe it with a fast hard timeout; if it does not answer a trivial
# scrape quickly, recycle the containers BEFORE the run so it self-heals. Only
# probes a LOCAL/self-hosted Firecrawl (skip when pointed at Firecrawl Cloud),
# and never aborts the run — a broken Firecrawl just degrades Stage 1.7.
FIRECRAWL_URL="${FIRECRAWL_BASE_URL:-http://localhost:3002}"
firecrawl_healthy() {
  curl -fsS --max-time 30 -X POST "${FIRECRAWL_URL%/}/v1/scrape" \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://example.com","formats":["markdown"],"timeout":15000}' \
    >/dev/null 2>&1
}
if command -v docker >/dev/null 2>&1 && [[ "$FIRECRAWL_URL" == *localhost* || "$FIRECRAWL_URL" == *127.0.0.1* ]]; then
  if firecrawl_healthy; then
    echo "[daily-update] Firecrawl health-gate: healthy" | tee -a "$PIPELINE_LOG"
  else
    echo "[daily-update] Firecrawl health-gate: UNHEALTHY/slow — recycling containers" | tee -a "$PIPELINE_LOG"
    docker restart firecrawl-api-1 firecrawl-playwright-service-1 >/dev/null 2>&1 || true
    for _i in $(seq 1 12); do firecrawl_healthy && break; sleep 5; done
    if firecrawl_healthy; then
      echo "[daily-update] Firecrawl health-gate: recovered after restart" | tee -a "$PIPELINE_LOG"
    else
      echo "[daily-update] Firecrawl health-gate: WARN still unhealthy after restart — Stage 1.7 may under-capture" | tee -a "$PIPELINE_LOG"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# Distributed lock — coordinate with GitHub Actions + any fallback runner.
# ---------------------------------------------------------------------------
# 2026-05-27: the daily pipeline is now triggered from two places by design:
#   1. This launchd job on the Mac mini.
#   2. .github/workflows/daily-scrape.yml on GitHub Actions (cron 10 UTC).
# Both call `python -m wekruit_matching.lock acquire` against the same
# Firestore doc so the second runner to arrive exits cleanly. Without the
# lock we'd double-write `matching-jobs` and double-spend on LLM enrichment.
LOCK_RUNNER="${SCRAPE_LOCK_RUNNER:-macmini-launchd@$(hostname -s)}"
LOCK_OUT="$(.venv/bin/python -m wekruit_matching.lock acquire --acquired-by "$LOCK_RUNNER" 2>&1)"
LOCK_RC=$?
echo "$LOCK_OUT"
if [[ "$LOCK_RC" -eq 2 ]]; then
  # Contended or already-run — another runner has today's lock, quiet exit.
  # We deliberately do not fire the post-pipeline webhook here; the
  # winning runner will fire its own.
  echo "[daily-update] lock contended; another runner owns today's scrape — exiting 0"
  exit 0
elif [[ "$LOCK_RC" -ne 0 ]]; then
  echo "[daily-update] lock acquire failed unexpectedly (rc=$LOCK_RC); aborting"
  exit "$LOCK_RC"
fi

# Append (not overwrite) so the runSha + alembic + preflight context recorded
# above stays in the same log file. The JOBS_* greps below use `tail -1` on the
# pipeline's own tokens, so the prefixed lines don't affect parsing. Exit-code
# semantics are unchanged from the original (success branch -> 0, else -> $?).
if .venv/bin/python -m wekruit_matching.pipeline.daily 2>&1 | tee -a "$PIPELINE_LOG"; then
  PIPELINE_RC=0
  STATUS="success"
else
  PIPELINE_RC=$?
  STATUS="failed"
fi

# pipeline.daily exits 1 on BOTH 'partial' (degraded result, core stages ok) and
# 'failed' (a core stage crashed/timed out). Prefer the precise pipelineStatus
# token it prints so a DEGRADED run is reported as 'partial', not a hard
# 'failed', to the webhook/operator. Falls back to the exit-code STATUS if the
# token is absent (older builds / a crash before the finalizer printed it).
PIPELINE_STATUS_TOKEN="$(grep -oE 'pipelineStatus=(success|partial|failed)' "$PIPELINE_LOG" | tail -1 | cut -d= -f2 || true)"
if [[ -n "$PIPELINE_STATUS_TOKEN" ]]; then
  STATUS="$PIPELINE_STATUS_TOKEN"
fi

FINISHED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

JOBS_SCRAPED="$(grep -oE 'jobsScraped[: =][0-9]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9]+' || echo 0)"
JOBS_NEW="$(grep -oE 'jobsNew[: =][0-9]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9]+' || echo 0)"
JOBS_UPDATED="$(grep -oE 'jobsUpdated[: =][0-9]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9]+' || echo 0)"
JOBS_ERRORED="$(grep -oE 'jobsErrored[: =][0-9]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9]+' || echo 0)"
COST_USD="$(grep -oE 'costUsd[: =][0-9.]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9.]+' || echo 0)"

ERROR_MSG=""
# Carry the degraded/failed/timeout stage list into the webhook's `error` field
# so the operator's pager shows WHY a run is partial/failed — not just the
# status. pipeline.daily prints stageOutcome.<stage>=<outcome>; these lines were
# previously computed then discarded before the webhook left the box (the gap
# that let a dead Serper reach no human for days).
DEGRADED_STAGES="$(grep -oE 'stageOutcome\.[a-z_]+=(degraded|error|timeout)' "$PIPELINE_LOG" 2>/dev/null | sed 's/^stageOutcome\.//' | sort -u | paste -sd ',' - 2>/dev/null || true)"
if [[ "$STATUS" == "failed" ]]; then
  ERROR_MSG="pipeline_exit_${PIPELINE_RC}"
fi
if [[ -n "$DEGRADED_STAGES" ]]; then
  if [[ -n "$ERROR_MSG" ]]; then
    ERROR_MSG="${ERROR_MSG}; stages: ${DEGRADED_STAGES}"
  else
    ERROR_MSG="degraded: ${DEGRADED_STAGES}"
  fi
fi

scripts/post-pipeline-webhook.sh \
  "$STATUS" \
  "$STARTED" \
  "$FINISHED" \
  "$JOBS_SCRAPED" \
  "$JOBS_NEW" \
  "$JOBS_UPDATED" \
  "$JOBS_ERRORED" \
  "$COST_USD" \
  "SimplifyJobs" \
  "$ERROR_MSG" \
  || true

# Release the lock with an audit-friendly stats blob. The CLI is tolerant
# of double-release, so a crash between here and the webhook call won't
# leave the lock dangling beyond the 4h stale window.
.venv/bin/python -m wekruit_matching.lock release \
  --outcome "$STATUS" \
  --stats-json "$(printf '{"jobsScraped":%s,"jobsNew":%s,"jobsUpdated":%s,"jobsErrored":%s,"costUsd":%s,"runner":"%s","runSha":"%s"}' \
      "$JOBS_SCRAPED" "$JOBS_NEW" "$JOBS_UPDATED" "$JOBS_ERRORED" "$COST_USD" "$LOCK_RUNNER" "$RUN_SHA")" \
  || echo "[daily-update] lock release failed — will be reclaimed as stale after 4h"

# Dead-man's-switch marker: record that a run COMPLETED today (any status). A
# separate launchd watchdog (scripts/check-daily-ran.sh, ~13:00 local) emails if
# this marker is missing/stale AND no run is in progress — catching a laptop that
# slept through 06:00 or a run that crashed before finishing. Durable path (not
# /tmp, which clears on reboot).
mkdir -p "$HOME/.wekruit" 2>/dev/null || true
printf '%s status=%s sha=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$STATUS" "$RUN_SHA" \
  > "$HOME/.wekruit/last-run" 2>/dev/null || true

exit $PIPELINE_RC
