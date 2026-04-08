---
phase: 18-observability-email-digest-testing
plan: "01"
subsystem: jd-pipeline
tags: [dashboard, email, observability, e2e]
---

# Phase 18 Plan 01 Summary

Completed the operator-facing observability layer for the JD pipeline.

## What Changed

- Expanded `internal_ui.py` pipeline status to show JD queue depth, failed attempts, coverage by source, and quality score buckets.
- Extended `notifications/email.py` to include JD attempt totals, Firecrawl credits used, and ATS-specific failure counts.
- Added `tests/test_internal_ui.py` assertions for the new pipeline sections.
- Added `tests/test_jd_pipeline_e2e.py` as a skip-safe live DB gate over the latest 1K jobs.

## Verification

- `uv run pytest tests/test_internal_ui.py tests/test_jd_pipeline_e2e.py -q`
- `uv run python -m py_compile src/wekruit_matching/api/internal_ui.py src/wekruit_matching/notifications/email.py tests/test_internal_ui.py tests/test_jd_pipeline_e2e.py`
