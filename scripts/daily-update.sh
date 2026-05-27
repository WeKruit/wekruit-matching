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

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PIPELINE_LOG="/tmp/wekruit-matching-daily-$(date -u +%Y%m%d-%H%M%S).log"

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

if .venv/bin/python -m wekruit_matching.pipeline.daily 2>&1 | tee "$PIPELINE_LOG"; then
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
  --stats-json "$(printf '{"jobsScraped":%s,"jobsNew":%s,"jobsUpdated":%s,"jobsErrored":%s,"costUsd":%s,"runner":"%s"}' \
      "$JOBS_SCRAPED" "$JOBS_NEW" "$JOBS_UPDATED" "$JOBS_ERRORED" "$COST_USD" "$LOCK_RUNNER")" \
  || echo "[daily-update] lock release failed — will be reclaimed as stale after 4h"

exit $PIPELINE_RC
