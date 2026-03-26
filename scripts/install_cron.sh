#!/usr/bin/env bash
# Install WeKruit cron jobs
# Run once: bash scripts/install_cron.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

SCRAPER_ENTRY="0 6 * * * bash $SCRIPT_DIR/cron_scraper.sh >> /tmp/wekruit_scraper.log 2>&1"
ENRICHMENT_ENTRY="30 6 * * * bash $SCRIPT_DIR/cron_enrichment.sh >> /tmp/wekruit_enrichment.log 2>&1"

# Load existing crontab (empty if none)
EXISTING_CRON=$(crontab -l 2>/dev/null || true)

UPDATED_CRON="$EXISTING_CRON"

# Add entries only if not already present
if ! echo "$EXISTING_CRON" | grep -qF "cron_scraper.sh"; then
    UPDATED_CRON="$UPDATED_CRON
$SCRAPER_ENTRY"
    echo "Added scraper cron: $SCRAPER_ENTRY"
else
    echo "Scraper cron already installed — skipping"
fi

if ! echo "$EXISTING_CRON" | grep -qF "cron_enrichment.sh"; then
    UPDATED_CRON="$UPDATED_CRON
$ENRICHMENT_ENTRY"
    echo "Added enrichment cron: $ENRICHMENT_ENTRY"
else
    echo "Enrichment cron already installed — skipping"
fi

echo "$UPDATED_CRON" | crontab -

echo ""
echo "Current crontab:"
crontab -l
