# Job Quality / Coverage Findings — 2026-05-29

Tool: `scripts/job_quality_report.py` (read-only, re-runnable). Run it anytime:
`uv run python scripts/job_quality_report.py [--samples] [--scope all]`

## Live coverage — 24,676 ACTIVE jobs

| Field | Coverage | Status |
|---|---|---|
| any apply URL | 100.0% | ✅ |
| seniority tag | 100.0% | ✅ (after normalize, below) |
| enriched_at | 99.7% | ✅ |
| job_description ≥200 | 99.6% | ✅ |
| industry | 98.8% | ✅ |
| **direct ATS link** | **17.5%** | structural gap |
| required_skills | 73.1% | mixed gap |
| company_size | 73.1% | extractor gap |
| **role_function** | **14.8%** | structural gap |
| embedded / **MATCHABLE** | **73.0%** | only 18,014 reach the live matcher |

## NOT 100%. Root causes (diagnosed on real data, not guessed)

### 1. direct ATS 17.5% — STRUCTURAL (source mix)
19,720 / 24,676 active jobs are jobright-sourced (`primary_url=jobright.ai`, a
redirect). Only 4,956 come from real ATS (ashby 1,581 / greenhouse 1,011 / lever
358 / stripe / airbnb …), of which 4,321 have a direct `ats_apply_url`. To raise
this we must RESOLVE each jobright redirect → real ATS URL (Serper/scrape) for
~19,720 jobs. A backfill project, not a quick fix. NOT a bug.

### 2. skills 73% & company_size 73% — MOSTLY low-skill-role + JD-QUALITY, not a
   simple extractor miss (CORRECTED after sampling)
6,546 of 6,600 skills-empty jobs HAVE a JD≥200, so at first it looked like an
extractor bug. But sampling the actual JDs shows they are predominantly non-tech
retail/service roles (TJX retail associate, Planet Fitness CSR, Circle K cashier,
bakery clerk) whose JD genuinely lists few hard skills, AND the jobright/firecrawl
"JD" is often polluted with UI chrome ("Hit enter to search or ESC to close", nav
links, "· 5 days ago") rather than clean description text. So:
  - blindly re-running the LLM on 6,500 jobs would waste calls and many stay empty
  - the real lever is JD-FETCH QUALITY (strip jobright UI noise before enrich) +
    accepting that low-skill roles legitimately have sparse skills
Company_size tracks the same rows (same JD-quality dependency).

### 3. role_function 14.8% — STRUCTURAL (jobright path never sets it)
jobright-newgrad 0/18,700, jobright-intern 0/1,020 populate role_function = 0.
Only ATS sources do (openai 179/194, stripe 164/183). The jobright enrichment
path simply doesn't emit role_function. Fix = add role_function inference to the
jobright enricher (cheap: derive from role_title, like seniority).

## FIXED THIS SESSION (live)
- **seniority off-vocab → canonical**: 3,507 active rows (mid_level/manager/
  director/Entry Level/'New Grad, Entry Level'/…) collapsed to intern|entry|mid|
  senior. WHY IT MATTERED: live CF matcher filters
  `where('seniorityLevel','==', userValue.toLowerCase())` — exact match, so those
  jobs could never match a user's entry/mid/senior filter (silent-hide bug, same
  class as sponsorship). Now off-vocab=0; dist mid 16,706 / entry 3,327 / senior
  3,195 / intern 1,448. Tool: `scripts/normalize_seniority_vocab.py` (reversible,
  idempotent). Backup: data/seniority_normalize_backup.tsv.

## RECOMMENDED NEXT (prioritized, none auto-run — each is real cost)
1. **role_function jobright backfill** (cheap, deterministic, +85% coverage of a
   tag): infer from role_title in the jobright enricher; mirror seniority backfill.
2. **JD-fetch noise strip** for jobright/firecrawl before enrich (raises skills +
   company_size quality without more LLM spend).
3. **jobright→ATS URL resolution** backfill (expensive: ~19,720 Serper/scrape) to
   raise direct-ATS from 17.5%.
4. Wire `job_quality_report` thresholds into the daily health-gate so coverage
   regressions on these fields alarm automatically.
