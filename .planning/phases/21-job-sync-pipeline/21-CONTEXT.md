# Phase 21 Context

- Phase 21 spans both repos:
  - `wekruit-matching` owns extraction, batching, and daily pipeline integration on the Mac Mini.
  - `wekruit-core-service-cloud-function` owns `POST /api/sync/jobs` and Firestore upsert logic.
- The contract is fixed by the pipeline side:
  - `X-API-Key` auth header
  - payload shape `{ collection, mode, jobs }`
  - jobs carry `content_hash`, inactive rows, and embeddings as JSON number arrays
- Core-service now stores query-ready derived fields (`jobType`, `locationBuckets`, `searchTokens`, `salaryMin`, `salaryMax`) so later phases do not need to rewrite sync output.
