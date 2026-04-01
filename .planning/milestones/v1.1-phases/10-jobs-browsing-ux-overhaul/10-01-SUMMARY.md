---
phase: 10-jobs-browsing-ux-overhaul
plan: "01"
subsystem: jobs-browser
tags: [jobs, responsive, pagination, status]
---

# Phase 10 Plan 01 Summary

Upgraded `/internal/jobs` and `/internal/jobs?status=inactive` from a desktop-only table pattern to a dual-layout browsing surface.

## What Changed

- Added one visible filter region with labels for search, status, source, and industry.
- Added a mobile card list that preserves core job fields without forcing horizontal-scroll-only browsing.
- Replaced `Y/--` style processing state with explicit badges: active/inactive, sponsorship, enriched, pending enrichment, embedded, pending embedding.
- Pagination now preserves filters through proper query encoding and uses true disabled spans at the boundaries.

## Verification

- `uv run pytest tests/test_internal_ui.py -q`
