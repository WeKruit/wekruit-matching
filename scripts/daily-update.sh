#!/bin/bash
# Daily job pipeline: scrape, enrich, embed + email notifications
# Runs via launchd at 6 AM CDT daily.
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

FINISHED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

JOBS_SCRAPED="$(grep -oE 'jobsScraped[: =][0-9]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9]+' || echo 0)"
JOBS_NEW="$(grep -oE 'jobsNew[: =][0-9]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9]+' || echo 0)"
JOBS_UPDATED="$(grep -oE 'jobsUpdated[: =][0-9]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9]+' || echo 0)"
JOBS_ERRORED="$(grep -oE 'jobsErrored[: =][0-9]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9]+' || echo 0)"
COST_USD="$(grep -oE 'costUsd[: =][0-9.]+' "$PIPELINE_LOG" | tail -1 | grep -oE '[0-9.]+' || echo 0)"

ERROR_MSG=""
if [[ "$STATUS" == "failed" ]]; then
  ERROR_MSG="pipeline_exit_${PIPELINE_RC}"
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

exit $PIPELINE_RC
