---
phase: 17-pipeline-orchestrator-daily-integration
plan: "01"
subsystem: jd-pipeline
tags: [orchestrator, daily, classifier, batching]
---

# Phase 17 Plan 01 Summary

Wired all JD enrichment tiers into the existing daily pipeline and propagated JD text into metadata classification.

## What Changed

- Added `src/wekruit_matching/pipeline/run_jd_enrichment.py` for batch queue processing, routing, per-domain throttling, and DB updates.
- Updated `src/wekruit_matching/pipeline/daily.py` to run JD enrichment as Stage 2b before metadata classification.
- Updated `models/job.py`, `enrichment/worker.py`, and `enrichment/classifier.py` so `job_description` participates in classification prompts.
- Added `tests/test_run_jd_enrichment.py` and extended `tests/test_enrichment_classifier.py`.

## Verification

- `uv run pytest tests/test_run_jd_enrichment.py tests/test_enrichment_classifier.py -q`
- `uv run python -m py_compile src/wekruit_matching/pipeline/run_jd_enrichment.py src/wekruit_matching/pipeline/daily.py src/wekruit_matching/enrichment/classifier.py src/wekruit_matching/enrichment/worker.py tests/test_run_jd_enrichment.py tests/test_enrichment_classifier.py`
