---
phase: 23-job-board-api
plan: "01"
subsystem: job-board
tags: [matching, job-board, firestore, indexes, search]
---

# Phase 23 Plan 01 Summary

Implemented the Firebase job board API and its Firestore index contract.

## What Changed

- Added `GET /api/matching/jobs` with cursor pagination, keyword search, sponsorship/industry/location filters, and post-query salary filtering over query-ready job documents.
- Added `GET /api/matching/jobs/:jobId` for single-read job detail retrieval.
- Added `searchTokens`, `requiredSkillsIndex`, `locationBuckets`, `salaryMin`, and `salaryMax` support in the read/query path.
- Declared Firestore composite indexes for the matching job corpus and updated repo docs so `matching-api` is formally registered alongside `outbound`.

## Caveats

- Salary filtering currently combines Firestore narrowing with post-query filtering; it is queryable but still needs live index-volume validation against production data.

## Verification

- `npm test`
- `rg -n "matching-api|platform-users|matching-jobs|MATCHING_OPENAI_API_KEY|/api/matching/jobs" /Users/wekruitclaw1/Desktop/WeKruit/wekruit-core-service-cloud-function/README.md /Users/wekruitclaw1/Desktop/WeKruit/wekruit-core-service-cloud-function/ARCHITECTURE.md /Users/wekruitclaw1/Desktop/WeKruit/wekruit-core-service-cloud-function/firestore.indexes.json`
