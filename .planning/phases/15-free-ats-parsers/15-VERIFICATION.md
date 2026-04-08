---
phase: 15-free-ats-parsers
verified: 2026-03-31T22:20:00-05:00
status: passed
score: 5/5 must-haves verified
re_verification: false
---

# Phase 15 Verification Report

## Goal Achievement

| Truth | Status | Evidence |
|-------|--------|----------|
| Greenhouse content is normalized to plain text with mapped department/location/salary | VERIFIED | `fetch_greenhouse_job()` maps `content`, `departments`, `offices`, and salary metadata |
| Lever lists and salary fields map to canonical JD shape | VERIFIED | `fetch_lever_job()` maps `descriptionPlain`, `lists`, `salaryRange`, and `workplaceType` |
| Ashby board feed maps description, compensation, and employment type | VERIFIED | `fetch_ashby_job()` uses `includeCompensation=true` and matches `jobUrl`/`applyUrl` |
| ATS text normalization removes HTML artifacts and zero-width noise | VERIFIED | `normalize_text()` is exercised directly in `tests/test_ats_enricher.py` |
| `data_quality_score` is deterministic and stored-ready | VERIFIED | `calculate_data_quality_score()` is tested against the full 100-point case and the schema now has `data_quality_score` |

## Automated Checks

- `uv run pytest tests/test_ats_enricher.py -q` — PASS
- `uv run python -m py_compile alembic/versions/0005_add_data_quality_score.py src/wekruit_matching/pipeline/ats_enricher.py tests/test_ats_enricher.py` — PASS
