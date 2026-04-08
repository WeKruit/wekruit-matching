---
phase: 21-job-sync-pipeline
plan: "01"
subsystem: mac-mini-to-firebase
tags: [matching, pipeline, firebase, firestore, content-hash]
---

# Phase 21 Plan 01 Summary

Implemented the job sync path across both repos.

## What Changed

- Added `wekruit_matching.pipeline.job_sync` to read active embedded jobs plus inactive stale jobs from Postgres and post batched payloads to Firebase.
- Wired the daily pipeline to run sync after embedding and added a one-time bulk loader script.
- Fixed the pipeline gap where a changed `content_hash` must clear prior embedding state so the job is re-embedded before incremental sync.
- Added the Firebase receiver `POST /api/sync/jobs` with batch-size validation, `content_hash`/status diffing, and Firestore upserts into `matching-jobs`.
- Derived query fields in Firestore job docs: `jobType`, `locationBuckets`, `searchTokens`, `requiredSkillsIndex`, `salaryMin`, and `salaryMax`.

## Caveats

- The one-time 47K bulk load script exists and is locally test-covered, but the full production run was not executed in this session.
- The DB-backed `clears_embedding_state` path is still skipped locally without a live `DATABASE_URL`.

## Verification

- `uv run pytest tests/test_pipeline_job_sync.py tests/test_pipeline_daily.py -q`
- `uv run pytest tests/test_scraper_upsert.py -q -k clears_embedding_state`
- `npm test`
