---
phase: quick
plan: 260401-eut
type: execute
wave: 1
depends_on: []
files_modified:
  - /Users/wekruitclaw1/Desktop/WeKruit/enrichment-validation-50.csv
autonomous: true
requirements: [VALIDATION-CSV]
---

<objective>
Revalidate `/Users/wekruitclaw1/Desktop/WeKruit/enrichment-validation-50.csv` by opening every JobRight URL in Playwright, then write a per-row score and note for each row.

Purpose: the validation sheet must reflect row-by-row human-auditable results, not just aggregate counts.
Output: every data row has J-M filled plus a note in column N; bottom summary clearly distinguishes total rows, expired rows, and live-page accuracy.
</objective>

<must_haves>
- Every one of the 41 data rows is checked from the rendered JobRight page via Playwright.
- Column N contains a per-row note for all 41 rows.
- Live rows receive an explicit per-row score derived from J-M.
- Expired rows are marked `N/A` and noted as expired.
- Bottom summary shows total rows, expired/N/A rows, live pages checked, and live-page accuracy.
</must_haves>

<tasks>
1. Inspect the current CSV structure and preserve the 41 URL-backed data rows only.
2. Re-open each JobRight URL in Playwright and validate rendered job content row by row.
3. Rewrite column N so every row includes an explicit score and note.
4. Rewrite the bottom summary so the denominators are unambiguous.
5. Verify that all 41 rows have notes and that summary counts match the row-level marks.
</tasks>
