# Phase 23 Context

- Firestore cannot full-text search company/title directly, so Phase 23 depends on query-shaping fields written during Phase 21:
  - `searchTokens`
  - `locationBuckets`
  - `requiredSkillsIndex`
  - `salaryMin` / `salaryMax`
- The job board had to stay inside the same `matching-api` function to preserve the single matching service boundary in core-service.
- Firestore composite indexes had to be declared in-repo so deployment can materialize the browse/query paths without one-off console work.
