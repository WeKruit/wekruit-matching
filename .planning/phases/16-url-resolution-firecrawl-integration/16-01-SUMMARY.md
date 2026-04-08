---
phase: 16-url-resolution-firecrawl-integration
plan: "01"
subsystem: jd-pipeline
tags: [workday, firecrawl, timeout, search]
---

# Phase 16 Plan 01 Summary

Implemented Workday and Firecrawl resolution for the long tail of JD fetches.

## What Changed

- Added optional Firecrawl config fields and environment examples.
- Added `src/wekruit_matching/pipeline/firecrawl_enricher.py` with `run_with_timeout()`, Workday CXS discovery, Firecrawl scrape/extract chaining, and search filtering.
- Added `tests/test_firecrawl_enricher.py` to verify timeout control, Workday CXS mapping, scrape-first behavior, extract escalation, and aggregator-aware search.

## Verification

- `uv run pytest tests/test_firecrawl_enricher.py -q`
- `uv run python -m py_compile src/wekruit_matching/pipeline/firecrawl_enricher.py tests/test_firecrawl_enricher.py`
