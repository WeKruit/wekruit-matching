# Phase 22 Context

- The TypeScript matching layer had to diverge from the current Python retrieval strategy:
  - Python uses pgvector ANN first, then Python-side filters.
  - v2.0 requires Firestore filters first, then in-memory cosine over the reduced result set.
- The scorer itself remained aligned with Python:
  - same 7 signals
  - same weights
  - same location alias behavior
  - same coverage-dominant skills-overlap formula
- Feedback state moved to Firestore collections:
  - `matching-feedback` for `like` / `dislike` / `applied`
  - `matching-saved-jobs` for bookmarks
