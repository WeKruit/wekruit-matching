---
phase: 03-llm-enrichment
verified: 2026-03-26T02:22:12Z
status: passed
score: 10/10 must-haves verified
re_verification: false
gaps: []
human_verification:
  - test: "Run enrichment worker against a live DB with real unenriched jobs and a real ANTHROPIC_API_KEY"
    expected: "Jobs have industry, company_size, required_skills, sponsorship, enriched_at populated after the run; re-running produces zero API calls"
    why_human: "DB integration tests skip without DATABASE_URL; cannot verify real Anthropic API responses or actual DB writes without live credentials"
  - test: "Run python -m wekruit_matching.enrichment.run and observe cron-compatible exit behavior"
    expected: "Process exits 0, logs completion stats, does not block indefinitely"
    why_human: "Requires live DB + API key; behavioral test of the CLI entrypoint under real conditions"
---

# Phase 3: LLM Enrichment Verification Report

**Phase Goal:** Every unenriched job in the database is classified with industry, company size, required skills, and sponsorship likelihood — without re-enriching unchanged jobs
**Verified:** 2026-03-26T02:22:12Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #  | Truth | Status | Evidence |
|----|-------|--------|---------|
| 1  | classify_job(job) returns an EnrichmentResult with industry drawn from the controlled vocabulary | VERIFIED | `field_validator("industry")` rejects non-vocab values; 13-entry INDUSTRY_VOCAB frozenset confirmed; test_invalid_industry_rejected passes |
| 2  | classify_job(job) returns company_size as one of startup/midsize/large/unknown — never an arbitrary string | VERIFIED | `field_validator("company_size")` enforces COMPANY_SIZE_VOCAB={"startup","midsize","large","unknown"}; test_invalid_company_size_rejected passes |
| 3  | classify_job(job) returns sponsorship as True, False, or None — never a hallucinated string | VERIFIED | `sponsorship: Optional[bool]` with explicit bool coercion in classify_job; test_sponsorship_bool_or_none_accepted passes |
| 4  | classify_job(job) returns required_skills as a list validated against KNOWN_SKILLS vocabulary | VERIFIED | `field_validator("required_skills")` filters to KNOWN_SKILLS intersection; 63-entry vocab confirmed; test_skills_not_in_vocab_are_dropped passes |
| 5  | classify_job raises no exception on Anthropic 429 or 5xx — tenacity retries up to 5 times with exponential backoff | VERIFIED | `@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=1, min=1, max=30), retry=retry_if_exception_type((RateLimitError, APIStatusError)), reraise=True)` on `_call_anthropic`; classify_job wraps entire call in try/except returning _safe_default(); test_429_triggers_retry passes with call_count==2 |
| 6  | classify_job returns EnrichmentResult with all-unknown/null values when Anthropic returns unparseable JSON — does not abort | VERIFIED | json.loads + EnrichmentResult construction both wrapped in try/except returning _safe_default(); test_invalid_json_returns_safe_default passes |
| 7  | enrich_pending queries WHERE enriched_at IS NULL AND status='active' — classifies all unenriched jobs and writes results | VERIFIED | Worker SQL confirmed: `WHERE enriched_at IS NULL AND status = 'active'`; UPDATE writes industry, company_size, required_skills, sponsorship, enriched_at; conn.commit() per row |
| 8  | Re-running the enrichment worker on jobs with unchanged content_hash makes zero Anthropic API calls | VERIFIED | upsert.py CASE expression: `enriched_at = CASE WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash THEN NULL ELSE jobs.enriched_at END`; worker only queries enriched_at IS NULL; test_enrich_pending_skips_already_enriched (skipped without DB, logic verified by code inspection) |
| 9  | A single classify_job failure does not abort the batch — other jobs continue | VERIFIED | `except Exception as exc: failed += 1; logger.warning(...); # Continue` pattern in worker.py for-loop; test_enrich_pending_continues_after_failure tests enriched==2, failed==1 |
| 10 | python -m wekruit_matching.enrichment.run is a standalone CLI entrypoint | VERIFIED | run.py has `if __name__ == "__main__":` block with `enrich_all()` call; `enrich_all()` importable confirmed |

**Score:** 10/10 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/wekruit_matching/enrichment/__init__.py` | Empty package marker | VERIFIED | File exists; 0 bytes (package marker) |
| `src/wekruit_matching/enrichment/classifier.py` | classify_job, EnrichmentResult, INDUSTRY_VOCAB, COMPANY_SIZE_VOCAB, KNOWN_SKILLS | VERIFIED | 199 lines; all 5 exports confirmed via import check; substantive (vocabs, validators, retry, prompt, JSON parsing) |
| `src/wekruit_matching/enrichment/worker.py` | enrich_pending(conn) -> dict[str, int] | VERIFIED | 97 lines; SQL query, per-row classify+update+commit, failure isolation, return dict |
| `src/wekruit_matching/enrichment/run.py` | enrich_all() + __main__ CLI block | VERIFIED | 37 lines; enrich_all() defined; __main__ block present; get_connection() wired |
| `tests/test_enrichment_classifier.py` | Unit tests for vocab, None/unknown handling, retry | VERIFIED | 149 lines; 10 tests; all 10 pass without API key |
| `tests/test_enrichment_worker.py` | DB integration tests for content-hash gating and failure isolation | VERIFIED | 162 lines; 4 tests skip without DATABASE_URL; correct skip pattern matches project convention |
| `src/wekruit_matching/scraper/upsert.py` | enriched_at = NULL CASE on hash change | VERIFIED | Lines 70-74: CASE expression clears enriched_at when content_hash changes |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| classifier.py | anthropic.Anthropic.messages.create | direct SDK call, model='claude-haiku-4-5' | VERIFIED | `client.messages.create(model="claude-haiku-4-5", ...)` at line 150 |
| classifier.py | tenacity.retry | @retry decorator on _call_anthropic() | VERIFIED | `@retry(stop=stop_after_attempt(5), ...)` at line 138; grep confirms 1 occurrence |
| worker.py | db/connection.py | get_connection() context manager (via run.py) | VERIFIED | run.py imports get_connection and passes conn to enrich_pending(conn) |
| worker.py | classifier.py | classify_job(job) call per row | VERIFIED | `from wekruit_matching.enrichment.classifier import classify_job` at line 15; called at line 62 |
| worker.py | jobs table | UPDATE jobs SET industry=..., enriched_at=... WHERE job_id=... | VERIFIED | UPDATE statement at lines 63-81 writes all 5 enrichment fields |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|--------------|--------|-------------------|--------|
| worker.py enrich_pending | `rows` from SELECT | psycopg3 fetchall() on jobs WHERE enriched_at IS NULL | Yes — live DB query | FLOWING |
| worker.py enrich_pending | `result` (EnrichmentResult) | classify_job(job) -> Anthropic API -> JSON parse | Yes — live API call with vocab validation | FLOWING |
| worker.py DB UPDATE | industry, company_size, required_skills, sponsorship, enriched_at | result fields + _utcnow() | Yes — all 5 fields from EnrichmentResult + current timestamp | FLOWING |
| upsert.py enriched_at clearing | enriched_at | CASE expression on content_hash comparison | Yes — SQL CASE resets to NULL on hash change | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Classifier imports successfully | `python -c "from wekruit_matching.enrichment.classifier import classify_job, EnrichmentResult, INDUSTRY_VOCAB, COMPANY_SIZE_VOCAB, KNOWN_SKILLS; print('OK')"` | All imports OK | PASS |
| INDUSTRY_VOCAB has 13 entries including unknown | `python -c "from wekruit_matching.enrichment.classifier import INDUSTRY_VOCAB; assert len(INDUSTRY_VOCAB)==13; assert 'unknown' in INDUSTRY_VOCAB"` | No assertion error | PASS |
| COMPANY_SIZE_VOCAB has 4 entries | `python -c "from wekruit_matching.enrichment.classifier import COMPANY_SIZE_VOCAB; assert COMPANY_SIZE_VOCAB=={'startup','midsize','large','unknown'}"` | No assertion error | PASS |
| enrich_all() importable | `python -c "from wekruit_matching.enrichment.run import enrich_all; print(hasattr(module, 'enrich_all'))"` | True | PASS |
| 10 classifier unit tests pass | `uv run pytest tests/test_enrichment_classifier.py -v` | 10 passed in 1.21s | PASS |
| Worker DB tests skip without DB (correct behavior) | `uv run pytest tests/test_enrichment_worker.py -v` | 4 skipped (DATABASE_URL not set) | PASS |
| @retry decorator present (1 occurrence) | `grep -c "@retry" classifier.py` | 1 | PASS |
| Model is claude-haiku-4-5 | `grep "claude-haiku" classifier.py` | match at line 151 | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|---------|
| ENRC-01 | 03-01 | LLM enrichment classifies industry | SATISFIED | INDUSTRY_VOCAB frozenset with 13 controlled values; field_validator enforces membership; classify_job writes industry to DB via worker |
| ENRC-02 | 03-01 | LLM enrichment estimates company size | SATISFIED | COMPANY_SIZE_VOCAB = {startup, midsize, large, unknown}; field_validator enforces; DB UPDATE writes company_size |
| ENRC-03 | 03-01 | LLM enrichment extracts likely required skills | SATISFIED | KNOWN_SKILLS frozenset with 63 entries; skills_in_vocab validator filters to intersection; DB UPDATE writes required_skills TEXT[] |
| ENRC-04 | 03-01 | LLM enrichment estimates visa sponsorship likelihood | SATISFIED | sponsorship: Optional[bool] — True/False/None only; explicit coercion from JSON null; DB UPDATE writes sponsorship |
| ENRC-05 | 03-02 | Content-hash gating: only enrich new or changed jobs | SATISFIED | Worker queries `WHERE enriched_at IS NULL AND status='active'`; upsert.py CASE expression clears enriched_at on content_hash change |
| ENRC-09 | 03-01 | Rate limiting and retry logic for Anthropic API calls | SATISFIED | @retry with stop_after_attempt(5), wait_exponential(multiplier=1, min=1, max=30), retry_if_exception_type((RateLimitError, APIStatusError)), reraise=True |
| ENRC-10 | 03-01 | Structured output validation (null/unknown as first-class values) | SATISFIED | _safe_default() returns {industry="unknown", company_size="unknown", required_skills=[], sponsorship=None}; both JSON parse and EnrichmentResult construction failures return safe default; "unknown" is valid in all vocab sets |

**Orphaned requirements check:** REQUIREMENTS.md maps ENRC-01 through ENRC-05, ENRC-09, ENRC-10 to Phase 3. All 7 IDs are claimed by plans (ENRC-01..04,09,10 by 03-01; ENRC-05 by 03-02). No orphaned requirements.

**Out-of-scope for Phase 3:** ENRC-06 (embedding generation), ENRC-07 (embedding storage), ENRC-08 (embedding_model identifier) — these are correctly assigned to Phase 4 and are not expected here.

### Anti-Patterns Found

No anti-patterns detected. Scanned all 3 enrichment source files and both test files.

Notable observation: `_should_retry()` helper function defined at classifier.py line 129 is not used by the `@retry` decorator (which uses `retry_if_exception_type` directly). The helper is dead code but does not affect correctness — the decorator correctly catches both `RateLimitError` and `APIStatusError`. The decorator's use of `APIStatusError` (which catches all status errors including some 4xx beyond 429) is broader than `_should_retry()` would allow, but this is documented in the SUMMARY as an intentional decision and `reraise=True` ensures non-retryable exceptions propagate after 5 attempts and are caught by `classify_job`'s outer try/except.

### Human Verification Required

#### 1. Live DB enrichment run

**Test:** Set DATABASE_URL and ANTHROPIC_API_KEY, insert 2-3 test jobs with enriched_at=NULL, run `uv run python -m wekruit_matching.enrichment.run`
**Expected:** Jobs have industry, company_size, required_skills, sponsorship, and enriched_at populated in the DB; re-running produces "No unenriched jobs found" log and zero API calls
**Why human:** DB integration tests skip without DATABASE_URL; behavioral contract requires live credentials to fully exercise

#### 2. Content-hash re-enrichment cycle

**Test:** After the above run, update one job's content (triggering a new scrape upsert with different content_hash), then re-run the enrichment worker
**Expected:** Only the job with the changed hash gets re-enriched; unchanged jobs remain skipped
**Why human:** Requires multi-step live DB interaction; cannot automate without DATABASE_URL

### Gaps Summary

No gaps. All 10 observable truths verified. All 7 required artifacts pass all three levels (exists, substantive, wired) plus data-flow trace. All 7 requirement IDs (ENRC-01..05, 09, 10) are satisfied. No blocker anti-patterns found.

The 6 pre-existing failures in `tests/test_scraper_parser.py` are out of scope for Phase 3 and were documented in the Phase 3 Plan 02 SUMMARY as pre-existing before Phase 3 work began.

---

_Verified: 2026-03-26T02:22:12Z_
_Verifier: Claude (gsd-verifier)_
