# Phase 4: Embeddings - Context

**Gathered:** 2026-03-25
**Status:** Ready for planning
**Mode:** Auto-generated (infrastructure phase — discuss skipped)

<domain>
## Phase Boundary

OpenAI text-embedding-3-small generation, pgvector storage, and HNSW index verification. Every enriched job gets a semantic embedding with model provenance tracked.

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion
All implementation choices are at Claude's discretion. Key decisions from research:

- Use OpenAI text-embedding-3-small (1536 dimensions, $0.02/1M tokens)
- Store embeddings in the existing vector(1536) column with HNSW index (already created in Phase 1 migration)
- Track embedding_model field for drift detection ("text-embedding-3-small")
- Content-hash gating: only embed jobs where embedded_at IS NULL or content_hash changed
- Use openai SDK (already installed) for embedding generation
- Batch embedding calls where possible (OpenAI supports batching)
- Store embedded_at timestamp

</decisions>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/wekruit_matching/models/job.py` — Job model with embedding field
- `src/wekruit_matching/config.py` — Settings with OPENAI_API_KEY
- `src/wekruit_matching/db/tables.py` — jobs table with vector(1536) column + HNSW index
- `src/wekruit_matching/enrichment/worker.py` — Pattern for content-hash gating worker

### Integration Points
- Reads enriched jobs from DB (WHERE embedded_at IS NULL AND enriched_at IS NOT NULL)
- Updates jobs.embedding + jobs.embedding_model + jobs.embedded_at

</code_context>

<specifics>
## Specific Ideas

Embedding text composition from spec: "{title} at {company}. Skills: {skills_list}"

</specifics>

<deferred>
## Deferred Ideas

None.

</deferred>
