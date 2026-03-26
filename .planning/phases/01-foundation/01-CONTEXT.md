# Phase 1: Foundation - Context

**Gathered:** 2026-03-25
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure phase — discuss skipped)

<domain>
## Phase Boundary

Project scaffolding, Postgres + pgvector schema, migrations, and environment config. The project is runnable and the database is ready to receive job data.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion — pure infrastructure phase. Use ROADMAP phase goal, success criteria, and codebase conventions to guide decisions.

Key technical decisions from research:
- Use uv for package management (not pip/poetry)
- Use psycopg3 (not psycopg2) for Postgres adapter
- Use pydantic v2 + pydantic-settings for config (not python-dotenv)
- Use alembic for migrations
- Use HNSW index (not IVFFlat) for pgvector
- Use ruff for linting/formatting

</decisions>

<code_context>
## Existing Code Insights

Greenfield project — no existing code. The project structure should follow the spec:
```
wekruit-matching/
├── src/wekruit_matching/   # Python package
├── .planning/              # GSD planning artifacts
├── .env.example            # Environment template
├── pyproject.toml          # Project metadata (uv)
└── alembic/                # Database migrations
```

</code_context>

<specifics>
## Specific Ideas

No specific requirements — infrastructure phase. Refer to ROADMAP phase description and success criteria.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>
