---
phase: 18-observability-email-digest-testing
verified: 2026-03-31T22:50:00-05:00
status: passed
score: 5/5 must-haves verified
re_verification: false
---

# Phase 18 Verification Report

## Goal Achievement

| Truth | Status | Evidence |
|-------|--------|----------|
| Pipeline page shows JD coverage segmented by source | VERIFIED | `pipeline_status()` now queries and renders `jd_fetch_source` coverage rows |
| Pipeline page exposes queue depth and failed attempts | VERIFIED | `pending_jd_queue` and `failed_fetches` are in the summary strip and backlog cards |
| Completion email includes JD attempts, credits, and ATS failure counts | VERIFIED | `send_pipeline_complete_email()` now consumes `jd_stats` with `credits_used` and `failed_by_source` |
| Pipeline page surfaces data quality distribution | VERIFIED | `data_quality_score` buckets render as metric cards in the new "Quality distribution" section |
| Latest-1K live DB gate exists for Greenhouse, Lever, and Ashby | VERIFIED | `tests/test_jd_pipeline_e2e.py` asserts at least one JD-ready job from each free ATS family, skipping cleanly without DB access |

## Automated Checks

- `uv run pytest tests/test_internal_ui.py tests/test_jd_pipeline_e2e.py -q` — PASS / SKIP-SAFE
- `uv run python -m py_compile src/wekruit_matching/api/internal_ui.py src/wekruit_matching/notifications/email.py tests/test_internal_ui.py tests/test_jd_pipeline_e2e.py` — PASS
