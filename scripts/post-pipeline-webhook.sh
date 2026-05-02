#!/bin/bash
# v1.5 Stream-A2 — POST signed webhook to wekruit-pa after daily-update.sh
set -euo pipefail

WEBHOOK_URL="${PA_MATCHING_WEBHOOK_URL:-https://us-central1-wekruit-5f89b.cloudfunctions.net/paMatchingPipelineComplete}"
SECRET="${PA_MATCHING_WEBHOOK_SECRET:-}"

if [[ -z "$SECRET" ]]; then
  echo "[post-pipeline-webhook] PA_MATCHING_WEBHOOK_SECRET unset — skipping" >&2
  exit 0
fi

STATUS="${1:-failed}"
STARTED="${2:?scrapeStartedAt required}"
FINISHED="${3:?scrapeFinishedAt required}"
JOBS_SCRAPED="${4:-0}"
JOBS_NEW="${5:-0}"
JOBS_UPDATED="${6:-0}"
JOBS_ERRORED="${7:-0}"
COST_USD="${8:-0}"
SOURCES_CSV="${9:-SimplifyJobs}"
ERROR_MSG="${10:-}"
RUN_ID="${11:-$(uuidgen | tr '[:upper:]' '[:lower:]')}"

SOURCES_JSON="[$(echo "$SOURCES_CSV" | sed 's/[^,][^,]*/"&"/g')]"

BODY=$(python3 -c "
import json
print(json.dumps({
    'runId': '$RUN_ID',
    'status': '$STATUS',
    'scrapeStartedAt': '$STARTED',
    'scrapeFinishedAt': '$FINISHED',
    'jobsScraped': int('$JOBS_SCRAPED'),
    'jobsNew': int('$JOBS_NEW'),
    'jobsUpdated': int('$JOBS_UPDATED'),
    'jobsErrored': int('$JOBS_ERRORED'),
    'costUsd': float('$COST_USD'),
    'sourceRepos': $SOURCES_JSON,
    'error': '$ERROR_MSG' if '$ERROR_MSG' else None,
}))
")

TS_MS=$(python3 -c 'import time; print(int(time.time()*1000))')
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -hex | awk '{print $NF}')

HTTP_CODE=$(curl -sS -o /tmp/pa-webhook-resp.json -w "%{http_code}" \
  -X POST "$WEBHOOK_URL" \
  -H "Content-Type: application/json" \
  -H "x-pa-signature: $SIG" \
  -H "x-pa-timestamp: $TS_MS" \
  --data-binary "$BODY" \
  --max-time 30 || echo "000")

echo "[post-pipeline-webhook] runId=$RUN_ID status=$STATUS http=$HTTP_CODE"
if [[ "$HTTP_CODE" != "200" ]]; then
  echo "[post-pipeline-webhook] response: $(cat /tmp/pa-webhook-resp.json 2>/dev/null || echo '<no body>')" >&2
fi
exit 0
