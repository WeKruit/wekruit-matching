# Phase 15: Free ATS Parsers - Context

**Gathered:** 2026-03-31  
**Status:** Ready for execution  
**Mode:** Autonomous

<domain>
## Phase Boundary

Implement zero-cost fetchers for Greenhouse, Lever, and Ashby, normalize their job-description payloads, and compute a data-quality score for downstream pipeline stages.

</domain>

<decisions>
## Implementation Decisions

- Use official public ATS endpoints only in this phase.
- Keep fetchers isolated from DB writes so the parser contract can be tested independently.
- Normalize all text through one utility path: HTML unescape, NFKC normalization, zero-width stripping, and HTML tag removal where needed.
- Add the `data_quality_score` schema field now so later orchestrator work can persist parser outputs without another structural detour.

</decisions>

<code_context>
## Existing Code Insights

- `job_description` already exists in the local table metadata and current worktree migration path.
- No dedicated ATS parser module exists yet; current enrichment paths are LLM- or JobRight-specific.
- Phase 14 routing now exists in `pipeline/url_classifier.py`, so parser selection can build on it cleanly.

</code_context>
