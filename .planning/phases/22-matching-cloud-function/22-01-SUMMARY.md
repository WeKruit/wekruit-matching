---
phase: 22-matching-cloud-function
plan: "01"
subsystem: matching-api
tags: [matching, firestore, cosine, scoring, feedback]
---

# Phase 22 Plan 01 Summary

Implemented the TypeScript matching engine on Firebase.

## What Changed

- Ported the 7-signal scorer to TypeScript with the same weights and behavioral formulas as the Python implementation.
- Added Firestore-first candidate retrieval, reducing the corpus with equality/array filters before cosine similarity runs in memory.
- Added OpenAI embedding integration for the query vector.
- Added Firestore-backed feedback persistence (`like`, `dislike`, `applied`) and saved-job persistence.
- Exposed `POST /api/matching/matches`, `POST /api/matching/feedback`, `POST /api/matching/saved-jobs`, and `DELETE /api/matching/saved-jobs/:userId/:jobId`.

## Caveats

- The `< 50ms over ~500 docs` runtime target was not benchmarked against a production-sized Firestore result set in this session.

## Verification

- `npm test`
