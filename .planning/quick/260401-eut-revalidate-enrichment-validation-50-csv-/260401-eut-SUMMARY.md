---
phase: quick
plan: 260401-eut
subsystem: validation
tags: [playwright, csv, manual-audit, jobright]
key_files:
  modified:
    - /Users/wekruitclaw1/Desktop/WeKruit/enrichment-validation-50.csv
decisions:
  - "Use Playwright-rendered pages as the source of truth; do not trust raw HTML fetches because JobRight intermittently serves login-wall content to non-browser requests."
  - "Write per-row scores into the existing Notes column instead of inventing a new CSV column."
  - "Treat expired pages as `N/A` rather than scoring them as failures."
metrics:
  rows_total: 41
  rows_live: 34
  rows_expired: 7
  jd_correct: 34
  skills_correct: 31
  sponsor_correct: 34
  salary_correct: 34
---

# Quick Task 260401-eut: Revalidate enrichment-validation-50.csv — Summary

## Outcome

The CSV was revalidated against Playwright-opened JobRight pages and rewritten so that every row now has a row-level note:

- `Score 4/4` for rows where JD, skills, sponsor, and salary all matched.
- `Score 3/4` for rows where only skills failed because parsed skills were blank while the page listed requirements.
- `Score N/A` for rows where the JobRight page was already expired.

## Final Counts

- Total rows: 41
- Expired / N/A rows: 7
- Live pages checked: 34
- JD accuracy: 34/34 live pages correct
- Skills accuracy: 31/34 live pages correct
- Sponsor accuracy: 34/34 live pages correct
- Salary accuracy: 34/34 live pages correct

## Notable Findings

- The only live-page extraction gap was skills coverage on 3 rows with blank parsed skills.
- Expired rows were preserved in the sheet and marked `N/A` with explicit notes.
- The summary now distinguishes live-page accuracy from total-row counts so the denominators are visible in Excel.
