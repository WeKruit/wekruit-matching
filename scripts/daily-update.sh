#!/bin/bash
# Daily job scraping, enrichment, and embedding update
# Runs via launchd at 6 AM CDT daily
set -e

cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching
LOG=/tmp/matching-daily-update.log

echo "=== $(date) Starting daily update ===" >> "$LOG"

# 1. Scrape both repos (upserts new/changed, marks stale)
.venv/bin/python -m wekruit_matching.scraper.run >> "$LOG" 2>&1

# 2. Enrich new/changed jobs (content-hash gated, skips unchanged)
.venv/bin/python -m wekruit_matching.enrichment.run >> "$LOG" 2>&1

# 3. Embed newly enriched jobs (skips already embedded)
.venv/bin/python -m wekruit_matching.embedding.run >> "$LOG" 2>&1

# 4. Purge inactive jobs older than 14 days
.venv/bin/python -m wekruit_matching.scraper.cleanup >> "$LOG" 2>&1

echo "=== $(date) Daily update complete ===" >> "$LOG"
