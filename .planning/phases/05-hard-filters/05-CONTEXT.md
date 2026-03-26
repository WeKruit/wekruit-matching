# Phase 5: Hard Filters - Context

**Gathered:** 2026-03-25
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure phase — discuss skipped)

<domain>
## Phase Boundary

Job type, sponsorship, and location pre-filtering with normalization. Callers can constrain matches to specific job types, sponsorship requirements, and locations before scoring runs.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
Key decisions:

- Location normalization: alias map for common abbreviations (SF/San Francisco, NYC/New York, LA/Los Angeles, etc.)
- Location matching: case-insensitive, handles "City, State" vs "City" vs state abbreviation
- "Remote" matches all location preferences
- Hard filters are SQL WHERE clauses applied before scoring
- Filter function takes a user profile dict and returns filtered job queryset
- This is a pure Python module — no new DB changes needed

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/wekruit_matching/models/user_profile.py` — UserProfile pydantic model with location_prefs, sponsorship_needed, job_type
- `src/wekruit_matching/db/tables.py` — jobs table with status, source_repo, sponsorship columns
- `src/wekruit_matching/db/connection.py` — psycopg3 connection

### Integration Points
- Filter output feeds into Phase 6 scoring engine
- Uses existing DB columns: status, job_type (mapped from source_repo), sponsorship, location_raw

</code_context>

<specifics>
## Specific Ideas

From spec: location normalization with these aliases:
- SF, San Francisco → san francisco
- NYC, New York → new york
- LA, Los Angeles → los angeles
- Remote → universal match

</specifics>

<deferred>
## Deferred Ideas

None.

</deferred>
