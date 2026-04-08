# Phase 14: DB Schema & URL Classifier - Context

**Gathered:** 2026-03-31  
**Status:** Ready for execution  
**Mode:** Autonomous

<domain>
## Phase Boundary

Add the JD fetch-tracking schema needed for the next pipeline stages and create a pure URL classifier that routes job links to the correct ATS tier before any network I/O.

This phase does not fetch job descriptions yet. It only establishes storage and routing truth.

</domain>

<decisions>
## Implementation Decisions

- Keep the existing untracked `0003` JD text migration intact and build Phase 14 as a follow-up `0004` migration so user work is not overwritten.
- Add only the fields required by the roadmap: `jd_fetch_source`, `jd_fetch_attempted_at`, and `ats_content_hash`.
- Put URL routing in a standalone pure-Python module with no DB or network dependency.
- Verify routing through unit tests covering ATS variants, tracking params, and unknown domains.

</decisions>

<code_context>
## Existing Code Insights

- `src/wekruit_matching/db/tables.py` already has local uncommitted additions for JD storage fields.
- `alembic/versions/0003_add_jd_enrichment_columns.py` exists in the worktree but is not in `HEAD`; Phase 14 must extend around it, not erase it.
- No `url_classifier.py` exists yet, and there are no tests for ATS routing.

</code_context>

<specifics>
## Specific Ideas

- Use a small enum-based router with route kinds for `greenhouse`, `lever`, `ashby`, `workday`, and `firecrawl`.
- Normalize URLs before matching so query params and fragments do not alter routing.
- Add a partial index on `(status, jd_fetch_attempted_at)` for rows where the JD text field is still empty.

</specifics>
