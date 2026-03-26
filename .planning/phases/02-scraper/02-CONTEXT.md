# Phase 2: Scraper - Context

**Gathered:** 2026-03-25
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure phase — discuss skipped)

<domain>
## Phase Boundary

GitHub README fetch, markdown parsing, stable ID generation, and upsert pipeline. Job listings are fetched from both SimplifyJobs repos, parsed correctly, and persisted to the database with stable IDs.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — infrastructure phase. Key research findings to incorporate:

- SimplifyJobs READMEs contain embedded HTML (`<details><summary>`) in cells — parser must handle this
- Continuation rows use `↳` arrow for same-company multi-role listings
- Emoji in Company column (🔥, 🔒) must be stripped before hashing for stable IDs
- GitHub raw content rate limits require authenticated requests (PAT via GITHUB_TOKEN env var)
- Content hash per job enables downstream enrichment gating (skip re-enrichment of unchanged jobs)
- Use httpx for HTTP requests (already in dependencies)
- Consider mistune for markdown table parsing (recommended by stack research)
- Upsert: ON CONFLICT (id) DO UPDATE for existing, mark stale jobs inactive (not delete)

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/wekruit_matching/models/job.py` — Job pydantic model with all fields
- `src/wekruit_matching/config.py` — Settings with DATABASE_URL, can add GITHUB_TOKEN
- `src/wekruit_matching/db/connection.py` — psycopg3 pool + connection context manager
- `src/wekruit_matching/db/tables.py` — SQLAlchemy jobs table definition

### Established Patterns
- pydantic v2 models for data validation
- pydantic-settings for environment config
- psycopg3 connection pool with context manager
- Atomic commits per logical unit

### Integration Points
- Scraper writes to jobs table via psycopg3 connection
- Content hash stored in jobs table for enrichment gating (Phase 3)

</code_context>

<specifics>
## Specific Ideas

Source URLs from spec:
- Internships: `https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/README.md`
- New Grad: `https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/README.md`

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>
