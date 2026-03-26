#!/usr/bin/env bash
# WeKruit scraper cron wrapper
# Scheduled: 0 6 * * * (6:00 AM ET)
# NOTE: Times are ET. If server timezone differs, adjust cron schedule accordingly.
# Usage: called by system cron or directly: bash scripts/cron_scraper.sh

set -euo pipefail

# Resolve project root relative to this script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Load .env if present (cron does not inherit shell environment)
if [[ -f "$PROJECT_ROOT/.env" ]]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Activate uv-managed venv
VENV="$PROJECT_ROOT/.venv"
if [[ ! -d "$VENV" ]]; then
    echo "ERROR: virtualenv not found at $VENV. Run: uv sync" >&2
    exit 1
fi
source "$VENV/bin/activate"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting scraper run"
python -m wekruit_matching.scraper.run
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Scraper run complete"
