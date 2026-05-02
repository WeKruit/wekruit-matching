#!/bin/bash
# Daily job pipeline: scrape, enrich, embed + email notifications
# Runs via launchd at 6 AM CDT daily.
# v1.5 Stream-A2: post-pipeline webhook to wekruit-pa.

cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
PIPELINE_LOG="/tmp/wekruit-matching-daily-$(date -u +%Y%m%d-%H%M%S).log"

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

exit $PIPELINE_RC
