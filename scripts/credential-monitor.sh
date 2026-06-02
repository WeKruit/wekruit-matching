#!/bin/bash
# Independent credential / external-dep auth probe.
#
# Purpose (CID-04 "whole-night-no-op" early-warning):
#   The nightly scrape can quietly degrade — or, before WS-B's preflight, fully
#   no-op — when an external credential dies (most painfully the Firestore
#   service-account key). This script runs the SAME preflight that
#   scripts/daily-update.sh runs, but on its OWN schedule a couple of hours
#   BEFORE the 6am run, so an operator is paged while there is still time to
#   rotate the key before the pipeline fires.
#
# It is intentionally standalone: it takes NO lock, writes NO data, and only
# probes dependency liveness. Safe to run as often as you like.
#
# Schedule it via launchd (see scripts/launchd-notes.md) a few hours before the
# daily-update.sh fire time.
#
# Exit codes (mirrors pipeline.preflight, re-emitted so a wrapping scheduler can
# branch too):
#   0 = all deps live (no alert sent)
#   2 = ONLY Firestore/sync credential down (degrade-class — alert sent so the
#       key can be rotated before the nightly run skips sync)
#   1 = hard fail / core dep down (alert sent)
#
# Override the alert status text with CREDENTIAL_MONITOR_DEGRADE=1 to treat a
# degrade (exit 2) as non-paging if you only care about hard fails.

# Resolve repo root from this script's location (same convention as
# daily-update.sh + install-laptop-scrape.sh) so it works on any account.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

NOW="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# Run the shared preflight, capturing its exit code without aborting on it.
set +e
PREFLIGHT_OUT="$(.venv/bin/python -m wekruit_matching.pipeline.preflight 2>&1)"
PREFLIGHT_RC=$?
set -e
echo "$PREFLIGHT_OUT"
echo "[credential-monitor] preflight rc=${PREFLIGHT_RC} at ${NOW}"

# Alert helper — reuse the SAME webhook path daily-update.sh uses so the
# operator's existing alert routing (wekruit-pa / mailgun downstream) fires.
# The webhook arg slots are fixed; we encode the probe verdict in the error
# field so it is unmistakable in the alert payload.
send_alert() {
  local reason="$1"
  echo "[credential-monitor] ALERTING: ${reason}" >&2
  scripts/post-pipeline-webhook.sh \
    "failed" \
    "$NOW" \
    "$NOW" \
    "0" "0" "0" "0" "0" \
    "SimplifyJobs" \
    "credential_monitor_${reason}" \
    || echo "[credential-monitor] WARN: alert webhook call failed (PA_MATCHING_WEBHOOK_SECRET unset?)" >&2
}

case "$PREFLIGHT_RC" in
  0)
    echo "[credential-monitor] OK — all deps live; no alert."
    exit 0
    ;;
  2)
    if [[ "${CREDENTIAL_MONITOR_DEGRADE:-1}" == "1" ]]; then
      send_alert "degrade_firestore_sync_down_rc2"
    else
      echo "[credential-monitor] degrade (rc=2) but CREDENTIAL_MONITOR_DEGRADE=0 — not alerting."
    fi
    exit 2
    ;;
  *)
    send_alert "hard_fail_rc_${PREFLIGHT_RC}"
    exit 1
    ;;
esac
