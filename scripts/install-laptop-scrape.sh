#!/usr/bin/env bash
# Install the daily-scrape launchd agent on a macOS laptop.
#
# 2026-05-27 — primary daily trigger after Adam ruled out the macmini
# launchd job (unreachable five days) and the GitHub Actions cron (cost).
# Pairs with the Firestore distributed lock in `src/wekruit_matching/lock.py`
# so multiple runners can never double-write `matching-jobs`.
#
# What it sets up:
#   1. `~/Library/LaunchAgents/com.wekruit.scrape.daily.plist` — launchd
#      job pointing at this repo's scripts/daily-update.sh, wrapped in
#      `caffeinate -is` so the laptop stays awake for the full run.
#   2. `pmset schedule wake` entry — wakes the laptop from sleep 5 minutes
#      before the launchd job fires (no-op if laptop is already awake).
#   3. `launchctl bootstrap` of the plist so it's active immediately.
#
# Usage:
#   bash scripts/install-laptop-scrape.sh           # install
#   bash scripts/install-laptop-scrape.sh --status  # show install state
#   bash scripts/install-laptop-scrape.sh --remove  # uninstall everything
#   bash scripts/install-laptop-scrape.sh --run-now # fire one immediately

set -euo pipefail

# Resolve repo root from this script's location.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$REPO_ROOT/scripts/laptop-fallback/com.wekruit.scrape.daily.plist.template"

LABEL="com.wekruit.scrape.daily"
TARGET_PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
WAKE_LABEL="WeKruitScrapeWake"

# 10:05 UTC default fire time. Adam can edit by re-running with
# WEKRUIT_SCRAPE_UTC_HOUR / _MINUTE set.
UTC_HOUR="${WEKRUIT_SCRAPE_UTC_HOUR:-10}"
UTC_MINUTE="${WEKRUIT_SCRAPE_UTC_MINUTE:-5}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

die() {
  echo "[install-laptop-scrape] ERROR: $*" >&2
  exit 1
}

note() {
  echo "[install-laptop-scrape] $*"
}

require_macos() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    die "This installer is macOS-only. For Linux/Docker fallback see DOCKER.md."
  fi
}

# Convert a UTC HH:MM into the laptop's *local* HH:MM via `date -u … -j`.
# StartCalendarInterval in launchd is interpreted in local time on Apple
# Silicon / macOS 13+, so we feed it the converted value.
utc_to_local() {
  local utc_hour="$1"
  local utc_minute="$2"
  # Use today's date to anchor the conversion (DST stays consistent for the
  # next ~6mo; re-run install-laptop-scrape.sh on DST flip if necessary).
  local today_utc
  today_utc="$(date -u +%Y-%m-%d)"
  # `date -j -f` parses the UTC stamp, prints in the laptop's local TZ.
  date -j -u -f "%Y-%m-%d %H:%M" "${today_utc} ${utc_hour}:${utc_minute}" "+%H %M"
}

render_plist() {
  local utc_hour="$1"
  local utc_minute="$2"
  local local_hm
  local_hm="$(utc_to_local "$utc_hour" "$utc_minute")"
  local local_hour="${local_hm% *}"
  local local_minute="${local_hm#* }"

  note "fire-time UTC ${utc_hour}:$(printf '%02d' "$utc_minute") = local ${local_hour}:${local_minute}"

  sed \
    -e "s|__REPO_ROOT__|${REPO_ROOT}|g" \
    -e "s|__USER_HOME__|${HOME}|g" \
    -e "s|__USER__|$(id -un)|g" \
    -e "s|__LOCAL_HOUR__|${local_hour}|g" \
    -e "s|__LOCAL_MINUTE__|${local_minute}|g" \
    "$TEMPLATE"
}

ensure_logs_dir() {
  install -d -m 700 "$HOME/Library/Logs"
}

# Schedule a daily pmset wake 5 minutes before the launchd fire so the
# laptop is alert when the job runs. `pmset repeat` overrides any prior
# entry — it's safe to re-run.
schedule_pmset_wake() {
  local utc_hour="$1"
  local utc_minute="$2"
  # Wake 5 minutes earlier.
  local wake_minute=$((utc_minute - 5))
  local wake_hour="$utc_hour"
  if (( wake_minute < 0 )); then
    wake_minute=$((wake_minute + 60))
    wake_hour=$((wake_hour - 1))
    if (( wake_hour < 0 )); then
      wake_hour=$((wake_hour + 24))
    fi
  fi
  local local_hm
  local_hm="$(utc_to_local "$wake_hour" "$wake_minute")"
  local local_hour="${local_hm% *}"
  local local_minute="${local_hm#* }"

  note "pmset wake daily at local ${local_hour}:${local_minute} (needs sudo)"
  # `pmset repeat wakeorpoweron MTWRFSU HH:MM:SS` — the day-string MTWRFSU
  # covers Mon-Sun. Re-running replaces the existing schedule.
  sudo pmset repeat wakeorpoweron MTWRFSU "${local_hour}:${local_minute}:00"
  note "current pmset schedule:"
  pmset -g sched | sed 's/^/  /'
}

clear_pmset_wake() {
  note "clearing pmset repeat schedule (needs sudo)"
  sudo pmset repeat cancel || true
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_install() {
  require_macos
  [[ -f "$TEMPLATE" ]] || die "template missing at $TEMPLATE — re-pull the repo"
  [[ -f "$REPO_ROOT/scripts/daily-update.sh" ]] || die "daily-update.sh missing"
  [[ -x "$REPO_ROOT/scripts/daily-update.sh" ]] || chmod +x "$REPO_ROOT/scripts/daily-update.sh"

  ensure_logs_dir

  install -d -m 700 "$(dirname "$TARGET_PLIST")"
  render_plist "$UTC_HOUR" "$UTC_MINUTE" > "$TARGET_PLIST"
  chmod 644 "$TARGET_PLIST"
  note "wrote $TARGET_PLIST"

  # Reload if already bootstrapped, otherwise bootstrap fresh.
  local domain="gui/$(id -u)"
  if launchctl print "${domain}/${LABEL}" >/dev/null 2>&1; then
    note "launchctl bootout (replacing existing job)"
    launchctl bootout "${domain}/${LABEL}" 2>/dev/null || true
  fi
  note "launchctl bootstrap"
  launchctl bootstrap "${domain}" "$TARGET_PLIST"
  launchctl enable "${domain}/${LABEL}"

  schedule_pmset_wake "$UTC_HOUR" "$UTC_MINUTE"

  note ""
  note "✅ installed."
  note "   verify:  bash scripts/install-laptop-scrape.sh --status"
  note "   fire:    bash scripts/install-laptop-scrape.sh --run-now"
  note "   logs:    tail -f ~/Library/Logs/wekruit-scrape-daily.out.log"
  note "   remove:  bash scripts/install-laptop-scrape.sh --remove"
}

cmd_status() {
  require_macos
  local domain="gui/$(id -u)"
  note "plist file:"
  ls -la "$TARGET_PLIST" 2>&1 | sed 's/^/  /'
  note "launchctl print:"
  launchctl print "${domain}/${LABEL}" 2>&1 | sed 's/^/  /' | head -40
  note "pmset schedule:"
  pmset -g sched | sed 's/^/  /'
  note "recent stdout (last 30 lines):"
  tail -n 30 "$HOME/Library/Logs/wekruit-scrape-daily.out.log" 2>/dev/null \
    | sed 's/^/  /' || note "  (no log yet)"
}

cmd_run_now() {
  require_macos
  local domain="gui/$(id -u)"
  note "kickstart now…"
  launchctl kickstart -k "${domain}/${LABEL}"
  note "  watch progress with: tail -f ~/Library/Logs/wekruit-scrape-daily.out.log"
}

cmd_remove() {
  require_macos
  local domain="gui/$(id -u)"
  if launchctl print "${domain}/${LABEL}" >/dev/null 2>&1; then
    note "launchctl bootout"
    launchctl bootout "${domain}/${LABEL}" 2>/dev/null || true
  fi
  if [[ -f "$TARGET_PLIST" ]]; then
    rm -v "$TARGET_PLIST"
  fi
  clear_pmset_wake
  note "✅ removed."
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

case "${1:-install}" in
  install) cmd_install ;;
  --status|status) cmd_status ;;
  --run-now|run-now) cmd_run_now ;;
  --remove|remove|uninstall) cmd_remove ;;
  -h|--help|help)
    sed -n '2,20p' "$0"
    ;;
  *)
    die "unknown command: $1 (try --help)"
    ;;
esac
