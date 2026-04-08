---
phase: 14-db-schema-url-classifier
plan: "01"
subsystem: jd-pipeline
tags: [schema, routing, migration, ats]
---

# Phase 14 Plan 01 Summary

Implemented the schema and pure routing foundation for JD enrichment.

## What Changed

- Added `0004_add_jd_fetch_tracking.py` for `jd_fetch_source`, `jd_fetch_attempted_at`, and `ats_content_hash`.
- Synced `src/wekruit_matching/db/tables.py` with the new fetch-tracking fields.
- Added `src/wekruit_matching/pipeline/url_classifier.py` for deterministic ATS routing with no network I/O.
- Added `tests/test_pipeline_url_classifier.py` to lock normalization and route selection.

## Verification

- `uv run pytest tests/test_pipeline_url_classifier.py -q`
- `uv run ruff check alembic/versions/0004_add_jd_fetch_tracking.py src/wekruit_matching/pipeline/url_classifier.py tests/test_pipeline_url_classifier.py src/wekruit_matching/db/tables.py`
- `uv run python -m py_compile alembic/versions/0004_add_jd_fetch_tracking.py src/wekruit_matching/pipeline/url_classifier.py tests/test_pipeline_url_classifier.py src/wekruit_matching/db/tables.py`
