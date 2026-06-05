#!/bin/bash
# Dead-man's switch for the daily pipeline (2026-06-05).
#
# The whole pipeline is laptop-bound (launchd + local Postgres + local
# Firecrawl). If the laptop sleeps through 06:00, or a run crashes/gets killed
# before finishing, NOTHING tells anyone — the only signals (completion email +
# webhook) fire on a *completed* run. This watchdog runs from a SEPARATE launchd
# job a few hours after the scheduled fire (~13:00 local) and emails if no run
# COMPLETED today and none is currently in progress.
#
# Install the companion launchd job:
#   ~/Library/LaunchAgents/com.wekruit.check.daily.plist  (StartCalendarInterval
#   Hour=13 Minute=0) running this script; then `launchctl load` it.
#
# Exit 0 = healthy (ran today or in progress); exit 1 = alerted.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

MARKER="$HOME/.wekruit/last-run"
TODAY="$(date -u +%Y-%m-%d)"

marker_is_today() {
  [[ -f "$MARKER" ]] && [[ "$(head -c 10 "$MARKER" 2>/dev/null)" == "$TODAY" ]]
}
run_in_progress() {
  pgrep -f "wekruit_matching.pipeline.daily" >/dev/null 2>&1
}

if marker_is_today; then
  echo "[check-daily-ran] OK — run completed today: $(cat "$MARKER" 2>/dev/null)"
  exit 0
fi
if run_in_progress; then
  echo "[check-daily-ran] OK — a run is currently in progress (slow but alive)"
  exit 0
fi

LAST="$([[ -f "$MARKER" ]] && cat "$MARKER" 2>/dev/null || echo 'never')"
echo "[check-daily-ran] ALERT — no completed run today (last completion: ${LAST})" >&2

# Settings() (pydantic) auto-loads .env for the Mailgun creds — do NOT `source
# .env` (unquoted JSON secret breaks the shell). Reuse the mid-run dependency
# alert primitive added for the Serper/Firecrawl reliability work.
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python - "$LAST" <<'PY' || echo "[check-daily-ran] alert send failed" >&2
import sys
from wekruit_matching.notifications.email import send_dependency_alert
last = sys.argv[1] if len(sys.argv) > 1 else "never"
send_dependency_alert(
    "Daily pipeline (laptop)",
    f"No pipeline run completed today and none is in progress. Last completion: {last}.",
    impact="Job listings are NOT being refreshed today — scrape/enrich/embed/sync did not run.",
    action=("Check the laptop was awake at 06:00 local. Inspect the latest "
            "/tmp/wekruit-matching-daily-*.log, or run scripts/daily-update.sh manually."),
)
print("[check-daily-ran] alert email sent")
PY
fi
exit 1
