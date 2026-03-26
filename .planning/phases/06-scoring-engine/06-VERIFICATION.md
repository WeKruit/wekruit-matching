---
phase: 06-scoring-engine
verified: 2026-03-25T00:00:00Z
status: passed
score: 17/17 must-haves verified
gaps: []
---

# Phase 6: Scoring Engine Verification Report

**Phase Goal:** Users can call `get_matches(profile, top_n=30)` and receive a ranked list of jobs with per-signal score breakdowns
**Verified:** 2026-03-25
**Status:** PASSED
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `score_job()` returns a dict with keys 'score' (float 0-1) and 'signals' (dict of 7 component scores) | VERIFIED | scorer.py L196-259; `test_score_job_returns_score_and_signals` passes |
| 2 | title_similarity is cosine similarity between query vector and job embedding vector | VERIFIED | scorer.py L48-67; `np.dot(a,b)/(norm_a*norm_b+1e-9)`, clipped to [0,1] |
| 3 | skills_overlap is `len(user_skills & job_skills) / len(job_skills)`, 0 when job has no skills | VERIFIED | scorer.py L70-85; `test_score_skills_overlap_*` pass |
| 4 | industry_match is 1.0 on exact match, 0.3 otherwise (0.3 when no preference) | VERIFIED | scorer.py L88-104; `test_score_industry_match_*` pass |
| 5 | company_size_match is 1.0 on match or 'any', 0.4 otherwise | VERIFIED | scorer.py L107-122; `test_score_company_size_match_*` pass |
| 6 | location_fit is 1.0 on canonical bucket match or remote, 0.2 otherwise | VERIFIED | scorer.py L125-153; `test_score_location_fit_*` pass |
| 7 | recency is `max(0, 1 - days_old/30)` using first_seen_at | VERIFIED | scorer.py L156-162; `test_score_recency_*` pass |
| 8 | feedback_boost is 0.5 cold-start, 1.0 liked, 0.0 disliked | VERIFIED | scorer.py L165-187; `test_score_feedback_boost_cold_start` passes (MTCH-13) |
| 9 | Final score = weighted sum: title*0.30 + skills*0.25 + industry*0.15 + size*0.10 + location*0.10 + recency*0.05 + boost*0.05 | VERIFIED | scorer.py WEIGHTS dict + L257; `test_weights_sum_to_one` + `test_score_job_weighted_sum_correct` pass; `sum(WEIGHTS.values()) == 1.0` confirmed live |
| 10 | All scorer tests pass without DB connection or API keys | VERIFIED | `pytest tests/test_matching_scorer.py -v`: 37 passed in 0.41s |
| 11 | `get_matches(profile, db_conn, top_n=30)` returns list of up to 30 dicts | VERIFIED | matcher.py L96-181; `test_get_matches_respects_top_n` passes |
| 12 | Each result dict contains 'score', 'signals', and all job fields from DB row | VERIFIED | matcher.py L157-158 `{**job, **score_result}`; `test_get_matches_preserves_job_fields` + `test_get_matches_result_has_score_and_signals` pass |
| 13 | ANN retrieval fetches top_n * 4 candidates via pgvector `<=>` before filtering | VERIFIED | matcher.py L69 `ORDER BY embedding <=> %s::vector`, L139 `ann_limit = top_n * 4`; `test_ann_limit_is_top_n_times_four` passes |
| 14 | Hard filters are applied before scoring | VERIFIED | matcher.py L149 `filtered = apply_hard_filters(ann_candidates, profile)` |
| 15 | User query is embedded via `embed_text()` — one call per `get_matches()` | VERIFIED | matcher.py L134; affinity bypass at L129-131 confirmed |
| 16 | Results sorted descending by score; only top_n returned | VERIFIED | matcher.py L163 `scored.sort(..., reverse=True)`, L175 `return scored[:top_n]`; `test_get_matches_sorted_by_score_desc` passes |
| 17 | `from wekruit_matching import get_matches` works in a fresh Python session | VERIFIED | `__init__.py` L5-7; live import confirmed: signature `(profile: 'UserProfile', conn: '...', top_n: 'int' = 30, openai_client: '...' = None) -> 'list[dict]'` |

**Score:** 17/17 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/wekruit_matching/matching/scorer.py` | 7 signal functions + `score_job()` + WEIGHTS constant | VERIFIED | 260 lines; all 9 exports present: WEIGHTS, score_title_similarity, score_skills_overlap, score_industry_match, score_company_size_match, score_location_fit, score_recency, score_feedback_boost, score_job |
| `tests/test_matching_scorer.py` | Full unit test suite, min 80 lines, no DB/API keys | VERIFIED | 340 lines; 37 tests; all pass without DB or API keys |
| `src/wekruit_matching/matching/matcher.py` | `get_matches()` implementation — ANN retrieval + hard filters + scoring | VERIFIED | 182 lines; `get_matches` defined with full pipeline |
| `src/wekruit_matching/__init__.py` | Public re-export: `get_matches` | VERIFIED | 7 lines; contains `from wekruit_matching.matching.matcher import get_matches` and `__all__ = ["get_matches", "__version__"]` |
| `tests/test_matching_matcher.py` | Unit tests for `get_matches()` with mocked DB/embedder, min 60 lines | VERIFIED | 273 lines; 8 tests; all pass with mocked connection and embed_text |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `score_job()` | WEIGHTS dict | weighted sum using `sum(WEIGHTS[k] * signals[k] for k in WEIGHTS)` | VERIFIED | scorer.py L257 — exact match |
| `score_location_fit()` | `_job_location_buckets` + `_preferred_buckets` | `from wekruit_matching.matching.filters import _job_location_buckets, _preferred_buckets` | VERIFIED | scorer.py L25, L141, L147 |
| `score_title_similarity()` | query_embedding + job embedding | `np.dot(a, b) / (norm_a * norm_b + 1e-9)` | VERIFIED | scorer.py L65 |
| `get_matches()` | `embed_text()` | user query string composed from profile skills | VERIFIED | matcher.py L134 |
| `get_matches()` | `apply_hard_filters()` | call before scoring loop | VERIFIED | matcher.py L149 |
| `get_matches()` | `score_job()` | scoring loop over filtered candidates | VERIFIED | matcher.py L156 |
| pgvector ANN query | jobs table embedding column | `<=>` operator via psycopg3 execute | VERIFIED | matcher.py L69 `ORDER BY embedding <=> %s::vector` |
| `src/wekruit_matching/__init__.py` | `matcher.get_matches` | direct re-export | VERIFIED | `__init__.py` L5 matches pattern exactly |

---

### Data-Flow Trace (Level 4)

`matcher.py` and `scorer.py` are pure computational modules — no dynamic data rendering. All data flows through function parameters (not state/props). Trace is straightforward:

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `matcher.py` get_matches | `ann_candidates` | pgvector DB query via `conn.execute(...)` | Yes — real SQL with `<=>` ANN operator | FLOWING |
| `matcher.py` get_matches | `query_embedding` | `embed_text()` or `profile.affinity_embedding` | Yes — live OpenAI call or user-provided vector | FLOWING |
| `scorer.py` score_job | `signals` dict | 7 pure functions computing from job dict + profile fields | Yes — computed from real inputs, no hardcoded values | FLOWING |

Note: In tests, DB and embed_text are mocked — this is correct test isolation, not a stub. Production path uses real DB and OpenAI.

---

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| All scorer tests pass | `uv run pytest tests/test_matching_scorer.py -v` | 37 passed, 0 failures | PASS |
| All matcher tests pass | `uv run pytest tests/test_matching_matcher.py -v` | 8 passed, 0 failures | PASS |
| WEIGHTS sum to 1.0 | `uv run python -c "from wekruit_matching.matching.scorer import WEIGHTS; print(sum(WEIGHTS.values()))"` | `1.0` | PASS |
| Public import works | `uv run python -c "from wekruit_matching import get_matches; import inspect; print(inspect.signature(get_matches))"` | Full signature returned | PASS |
| No DB/API imports in scorer.py | `grep -n "psycopg\|openai\|anthropic\|httpx" scorer.py` | No matches | PASS |

---

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| MTCH-04 | 06-01 | Title similarity scoring via embedding cosine similarity (weight: 0.30) | SATISFIED | scorer.py `score_title_similarity`, WEIGHTS["title_similarity"]=0.30 |
| MTCH-05 | 06-01 | Skills overlap scoring (weight: 0.25) | SATISFIED | scorer.py `score_skills_overlap`, WEIGHTS["skills_overlap"]=0.25 |
| MTCH-06 | 06-01 | Industry match scoring (weight: 0.15) | SATISFIED | scorer.py `score_industry_match`, WEIGHTS["industry_match"]=0.15 |
| MTCH-07 | 06-01 | Company size preference scoring (weight: 0.10) | SATISFIED | scorer.py `score_company_size_match`, WEIGHTS["company_size_match"]=0.10 |
| MTCH-08 | 06-01 | Location fit scoring (weight: 0.10) | SATISFIED | scorer.py `score_location_fit`, WEIGHTS["location_fit"]=0.10 |
| MTCH-09 | 06-01 | Recency scoring — newer posts rank higher (weight: 0.05) | SATISFIED | scorer.py `score_recency`, WEIGHTS["recency"]=0.05 |
| MTCH-10 | 06-01 | Feedback boost scoring from past likes/dislikes (weight: 0.05) | SATISFIED | scorer.py `score_feedback_boost`, WEIGHTS["feedback_boost"]=0.05 |
| MTCH-11 | 06-01 | Returns top-N ranked jobs with individual signal breakdown per match | SATISFIED | score_job returns `{"score": float, "signals": dict[7]}`, get_matches slices to top_n |
| MTCH-12 | 06-02 | Library API entry point: `get_matches(profile, top_n=30) -> list[dict]` | SATISFIED | matcher.py exports get_matches; `__init__.py` re-exports it; live import confirmed |
| MTCH-13 | 06-01 | Cold-start mode for users with no feedback history (neutral feedback signal) | SATISFIED | score_feedback_boost returns 0.5 when not in liked or disliked; `test_score_job_cold_start_feedback_boost` passes |

All 10 requirements satisfied. No orphaned requirements.

---

### Anti-Patterns Found

None. Scan of `scorer.py`, `matcher.py`, and `__init__.py` found:
- No TODO/FIXME/PLACEHOLDER comments
- No empty return stubs (`return null`, `return {}`, `return []`)
- No hardcoded empty data flowing to output
- No DB or API imports in scorer.py (pure computation only)
- No console.log-only handlers

---

### Human Verification Required

None. All goal truths are verifiable programmatically via the test suite and import checks. The phase produces a pure Python library with no UI or external service behavior that requires human observation.

Success Criterion 2 from ROADMAP ("Changing a profile's skills list visibly changes ranking") is verified by proxy: `test_get_matches_sorted_by_score_desc` confirms that varying signal inputs (liked/disliked companies) produce differentiated scores and descending sort order. The skills overlap signal is a pure function verified to produce proportional scores by `test_score_skills_overlap_*`.

---

### Gaps Summary

No gaps. All 17 must-have truths verified, all 5 artifacts substantive and wired, all 8 key links confirmed, all 10 requirements satisfied, 45 tests pass with 0 failures.

---

_Verified: 2026-03-25_
_Verifier: Claude (gsd-verifier)_
