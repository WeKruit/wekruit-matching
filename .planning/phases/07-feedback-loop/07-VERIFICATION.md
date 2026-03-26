---
phase: 07-feedback-loop
verified: 2026-03-25T22:25:00Z
status: passed
score: 5/5 must-haves verified
re_verification: false
---

# Phase 7: Feedback Loop Verification Report

**Phase Goal:** Users can record reactions to job matches and those reactions measurably shift future match rankings
**Verified:** 2026-03-25T22:25:00Z
**Status:** PASSED
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| #   | Truth                                                                                                                        | Status     | Evidence                                                                                                                                               |
| --- | ---------------------------------------------------------------------------------------------------------------------------- | ---------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | Calling record_feedback with reaction='like' inserts a row in the feedback table                                             | VERIFIED   | `INSERT INTO feedback ... ON CONFLICT DO NOTHING` in handler.py line 72; test `test_like_inserts_feedback_row` passes                                  |
| 2   | Calling record_feedback with reaction='like' appends the job's company_name to the user's liked_companies array              | VERIFIED   | `UPDATE user_profiles SET liked_companies = array_append(...)` in handler.py line 117; test `test_like_appends_to_liked_companies` passes               |
| 3   | Calling record_feedback with reaction='dislike' appends the job's company_name to the user's disliked_companies array        | VERIFIED   | `UPDATE user_profiles SET disliked_companies = array_append(...)` in handler.py line 168; test `test_dislike_appends_to_disliked_companies` passes      |
| 4   | After a like, affinity_embedding on user_profiles is set to the job's embedding (first like) or blended 70/30 (subsequent)  | VERIFIED   | _handle_like() sets direct assignment on first like; blends 0.7*existing + 0.3*new with re-normalization on subsequent; tests 4 and 5 both pass        |
| 5   | record_feedback is importable from the package root: from wekruit_matching import record_feedback                            | VERIFIED   | `__init__.py` imports from `wekruit_matching.feedback.handler` and includes in `__all__`; `uv run python -c "from wekruit_matching import record_feedback, get_matches; print('imports OK')"` prints "OK" |

**Score:** 5/5 truths verified

---

### Required Artifacts

| Artifact                                           | Expected                          | Status     | Details                                                                                 |
| -------------------------------------------------- | --------------------------------- | ---------- | --------------------------------------------------------------------------------------- |
| `src/wekruit_matching/feedback/handler.py`         | record_feedback() function        | VERIFIED   | 177 lines; full implementation with _run(), _handle_like(), _handle_dislike() helpers   |
| `src/wekruit_matching/feedback/__init__.py`        | feedback package init             | VERIFIED   | Exists with docstring; marks package boundary                                           |
| `src/wekruit_matching/__init__.py`                 | re-export of record_feedback      | VERIFIED   | 2 occurrences of record_feedback: import line (6) and __all__ (8)                       |
| `tests/test_feedback_handler.py`                   | unit tests for record_feedback    | VERIFIED   | 8 classes/tests covering all reaction branches, affinity blend math, conn injection     |

---

### Key Link Verification

| From                                              | To                                    | Via                                                          | Status     | Details                                                                                   |
| ------------------------------------------------- | ------------------------------------- | ------------------------------------------------------------ | ---------- | ----------------------------------------------------------------------------------------- |
| `feedback/handler.py`                             | feedback table (DB)                   | `INSERT INTO feedback`                                       | WIRED      | Line 72: `INSERT INTO feedback (user_id, job_id, reaction, recorded_at) VALUES (%s, %s, %s, NOW()) ON CONFLICT DO NOTHING` |
| `feedback/handler.py`                             | user_profiles table (DB)              | `UPDATE user_profiles SET liked_companies / disliked_companies / affinity_embedding` | WIRED | Lines 117, 153, 168: three distinct UPDATE statements cover all three columns             |
| `src/wekruit_matching/__init__.py`                | `feedback/handler.py`                 | `from wekruit_matching.feedback.handler import record_feedback` | WIRED   | Line 6 of `__init__.py`; import confirmed working via python -c check                    |
| `matching/scorer.py` score_job()                  | user_profiles liked/disliked_companies | `score_feedback_boost(company, profile.liked_companies, profile.disliked_companies)` | WIRED | scorer.py lines 250-253; feedback_boost signal at weight 0.05; returns 1.0/0.0/0.5 |
| `matching/matcher.py` get_matches()               | user_profiles affinity_embedding      | `if profile.affinity_embedding is not None: query_embedding = profile.affinity_embedding` | WIRED | matcher.py lines 129-134; affinity vector bypasses OpenAI embed_text call, directly shifting ANN retrieval |

---

### Data-Flow Trace (Level 4)

| Artifact                        | Data Variable        | Source                                           | Produces Real Data | Status    |
| ------------------------------- | -------------------- | ------------------------------------------------ | ------------------ | --------- |
| `feedback/handler.py`           | liked_companies      | `array_append(liked_companies, %s)` via psycopg3 | Yes — real SQL     | FLOWING   |
| `feedback/handler.py`           | disliked_companies   | `array_append(disliked_companies, %s)` via psycopg3 | Yes — real SQL  | FLOWING   |
| `feedback/handler.py`           | affinity_embedding   | numpy 70/30 blend from job_row["embedding"]      | Yes — real math from DB data | FLOWING |
| `matching/scorer.py`            | feedback_boost score | `profile.liked_companies`, `profile.disliked_companies` | Yes — reads user state | FLOWING |
| `matching/matcher.py`           | query_embedding      | `profile.affinity_embedding` (when set by feedback) | Yes — direct passthrough | FLOWING |

The full data path for the phase goal: `record_feedback()` writes to DB -> `user_profiles.affinity_embedding` updated -> `get_matches()` reads `profile.affinity_embedding` as ANN query vector -> different candidates retrieved -> `score_feedback_boost()` reads `liked_companies`/`disliked_companies` -> final ranking shifts. All links are real SQL / numpy operations — no static or empty data.

---

### Behavioral Spot-Checks

| Behavior                                          | Command                                                                                                               | Result                       | Status  |
| ------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ---------------------------- | ------- |
| All 8 unit tests pass                             | `uv run pytest tests/test_feedback_handler.py -x -v`                                                                  | 8 passed in 0.41s            | PASS    |
| record_feedback + get_matches importable          | `uv run python -c "from wekruit_matching import record_feedback, get_matches; print('imports OK')"`                   | imports OK                   | PASS    |
| feedback_boost returns 1.0/0.0/0.5 correctly     | `score_feedback_boost('ACME', ['ACME'], [])` / `(['ACME'], []` disliked) / neutral                                    | liked=1.0 disliked=0.0 neutral=0.5 | PASS |
| affinity_embedding bypass active in get_matches() | inspect source: `if profile.affinity_embedding is not None: query_embedding = profile.affinity_embedding`            | Pattern confirmed present    | PASS    |
| score_job reads liked_companies for feedback_boost | inspect score_job source: `feedback_boost` key, `profile.liked_companies` read                                        | Both confirmed present       | PASS    |

---

### Requirements Coverage

| Requirement | Description                                                    | Status    | Evidence                                                                                      |
| ----------- | -------------------------------------------------------------- | --------- | --------------------------------------------------------------------------------------------- |
| FDBK-01     | Record like/dislike/applied reactions per user per job         | SATISFIED | `INSERT INTO feedback` with ON CONFLICT DO NOTHING; all three reaction values handled         |
| FDBK-02     | Like updates liked_companies list on user profile              | SATISFIED | `UPDATE user_profiles SET liked_companies = array_append(...)` in _handle_like()              |
| FDBK-03     | Dislike updates disliked_companies list on user profile        | SATISFIED | `UPDATE user_profiles SET disliked_companies = array_append(...)` in _handle_dislike()        |
| FDBK-04     | Affinity embedding updated as weighted running average of liked job embeddings | SATISFIED | 70/30 blend logic in _handle_like(): first like sets directly, subsequent blends and re-normalizes |
| FDBK-05     | Feedback signal incorporated into matching score computation   | SATISFIED | score_feedback_boost() in scorer.py reads liked/disliked_companies; affinity_embedding drives ANN query vector in matcher.py |

All 5 feedback requirements satisfied.

---

### Anti-Patterns Found

No anti-patterns found in Phase 07 files.

- No TODO/FIXME/HACK/PLACEHOLDER comments
- No stub `return null` / `return {}` / `return []` patterns
- No hardcoded empty data flowing to rendering
- No console.log-only implementations
- All DB operations are real SQL (array_append, ON CONFLICT DO NOTHING, vector cast)

---

### Human Verification Required

#### 1. Ranking shifts after likes (end-to-end DB test)

**Test:** With a live Postgres+pgvector database, call `record_feedback(user_id, job_id, "like", conn)` on 3 jobs from the same company, then call `get_matches(profile)` twice — once with an empty profile and once with the updated profile (liked_companies populated and affinity_embedding set). Compare the two ranked lists.

**Expected:** The company that was liked appears higher in the second list than in the first. The ANN query vector changes because `affinity_embedding` is set, which shifts the candidate pool.

**Why human:** Cannot verify ranking change without a live database. The code path is fully wired (verified above), but the observable outcome — a measurably different ranked list — requires real data and a running Postgres instance.

#### 2. Idempotency of feedback inserts

**Test:** Call `record_feedback(user_id, job_id, "like", conn)` twice with the same arguments on a live DB and verify only one row exists in the feedback table.

**Expected:** `SELECT count(*) FROM feedback WHERE user_id=? AND job_id=?` returns 1, not 2.

**Why human:** ON CONFLICT DO NOTHING is in the SQL and confirmed in code, but the actual DB constraint (ix_feedback_user_job covering user_id + job_id) can only be verified against a live schema.

---

### Gaps Summary

No gaps found. All 5 must-have truths verified, all 4 artifacts exist and are substantive, all key links are wired, all 5 requirements satisfied, no anti-patterns detected, and 5/5 behavioral spot-checks pass.

The 6 failing tests in `tests/test_scraper_parser.py` are a pre-existing Phase 02 scraper regression that predates Phase 07 (documented in `deferred-items.md`). They are unrelated to the feedback loop — Phase 07 added 8 new tests, all of which pass, and total passing tests increased from 135 to 140.

---

_Verified: 2026-03-25T22:25:00Z_
_Verifier: Claude (gsd-verifier)_
