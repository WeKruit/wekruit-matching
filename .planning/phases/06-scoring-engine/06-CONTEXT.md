# Phase 6: Scoring Engine - Context

**Gathered:** 2026-03-25
**Status:** Ready for planning
**Mode:** Auto-generated (discuss skipped)

<domain>
## Phase Boundary

7-signal weighted scoring, ranked results API (`get_matches()`), and cold-start handling. Users can call `get_matches(profile, top_n=30)` and receive a ranked list of jobs with per-signal score breakdowns.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
Key decisions from spec:

- Scoring weights: title_similarity 0.30, skills_overlap 0.25, industry_match 0.15, company_size_match 0.10, location_fit 0.10, recency 0.05, feedback_boost 0.05
- Title similarity uses embedding cosine similarity (user query embedding vs job embedding)
- Skills overlap: user_skills intersection with job required_skills / len(job_skills)
- Industry match: 1.0 if match, 0.3 for adjacent/other
- Company size match: 1.0 if match or "any", 0.4 otherwise
- Location fit: 1.0 if match or remote, 0.2 otherwise (reuse Phase 5 normalize_location)
- Recency: max(0, 1 - days_old/30)
- Feedback boost: neutral (0.5) for cold-start, boosted for liked companies, penalized for disliked
- Flow: hard_filters → ANN retrieval via pgvector → weighted scoring → return top-N
- Each match result includes `signals` dict with individual component scores
- Cold-start: feedback_boost = 0.5 (neutral), other signals drive ranking
- get_matches() is a library function, not HTTP endpoint
- Needs user query embedding generation for title similarity

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/wekruit_matching/matching/filters.py` — apply_hard_filters(), normalize_location()
- `src/wekruit_matching/embedding/embedder.py` — embed_text(), compose_embedding_text()
- `src/wekruit_matching/models/user_profile.py` — UserProfile with all preference fields
- `src/wekruit_matching/models/job.py` — Job model
- `src/wekruit_matching/db/connection.py` — get_connection()

### Integration Points
- Reads jobs from DB with embedding vectors for cosine similarity
- Uses Phase 5 hard filters as pre-scoring step
- Uses Phase 4 embedding module for user query embedding

</code_context>

<specifics>
## Specific Ideas

Public API from spec:
```python
def get_matches(profile: dict, db, top_n: int = 30) -> list[dict]:
```
Returns list of dicts with `score`, `signals`, and job fields.

</specifics>

<deferred>
## Deferred Ideas

None.

</deferred>
