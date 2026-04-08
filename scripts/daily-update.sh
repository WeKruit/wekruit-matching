#!/bin/bash
# Daily job pipeline: scrape, enrich, embed + email notifications
# Runs via launchd at 6 AM CDT daily
cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching

.venv/bin/python -m wekruit_matching.pipeline.daily
