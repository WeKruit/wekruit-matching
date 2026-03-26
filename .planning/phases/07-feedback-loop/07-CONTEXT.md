# Phase 7: Feedback Loop - Context

**Gathered:** 2026-03-25
**Status:** Ready for planning
**Mode:** Auto-generated (discuss skipped)

<domain>
## Phase Boundary

Like/dislike recording, affinity embedding updates, and preference propagation. Users can record reactions to job matches and those reactions measurably shift future match rankings.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
From spec:

- record_feedback(user_id, job_id, reaction, db) — records like/dislike/applied
- Like: adds company to liked_companies, updates affinity_embedding (70% existing + 30% new signal, re-normalized)
- Dislike: adds company to disliked_companies
- Affinity embedding: running weighted average of liked job embeddings
- First like (no prior affinity): job's embedding becomes the initial affinity
- feedback_boost signal in scorer already handles the scoring side (Phase 6)
- This phase just provides the data mutation — record_feedback function
- Export record_feedback from package root alongside get_matches

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/wekruit_matching/models/feedback.py` — Feedback pydantic model
- `src/wekruit_matching/models/user_profile.py` — UserProfile with liked_companies, disliked_companies, affinity_embedding
- `src/wekruit_matching/db/tables.py` — feedback table, user_profiles table
- `src/wekruit_matching/db/connection.py` — get_connection()
- `src/wekruit_matching/matching/scorer.py` — score_feedback_boost already reads liked/disliked companies

### Integration Points
- Writes to feedback table and user_profiles table
- Affinity embedding feeds into get_matches() via scorer's feedback_boost signal

</code_context>

<specifics>
## Specific Ideas

None beyond spec.

</specifics>

<deferred>
## Deferred Ideas

- Feedback decay (v2 — ADV-01)
- Diversity injection (v2 — ADV-02)

</deferred>
