---
phase: 15-free-ats-parsers
plan: "01"
subsystem: jd-pipeline
tags: [greenhouse, lever, ashby, normalization, quality]
---

# Phase 15 Plan 01 Summary

Implemented the free ATS parser layer and quality scoring.

## What Changed

- Added `0005_add_data_quality_score.py` plus metadata sync in `db/tables.py`.
- Added `src/wekruit_matching/pipeline/ats_enricher.py` with normalization helpers, canonical ATS result mapping, and data quality scoring.
- Implemented Greenhouse, Lever, and Ashby fetchers using public endpoints.
- Added `tests/test_ats_enricher.py` for normalization, field mapping, and exact score behavior.

## Verification

- `uv run pytest tests/test_ats_enricher.py -q`
- `uv run python -m py_compile alembic/versions/0005_add_data_quality_score.py src/wekruit_matching/pipeline/ats_enricher.py tests/test_ats_enricher.py`
