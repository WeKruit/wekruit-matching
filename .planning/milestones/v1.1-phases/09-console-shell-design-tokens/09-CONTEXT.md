# Phase 9: Console Shell & Design Tokens - Context

**Gathered:** 2026-03-31  
**Status:** Ready for execution  
**Mode:** Autonomous fallback (nested repo GSD root conflict)

<domain>
## Phase Boundary

Establish the shared WeKruit console shell, tokenized styling foundation, and semantic page structure for `/internal/jobs`, `/internal/stats`, and `/internal/pipeline`.

This phase does not change matching logic, data pipelines, or auth.

</domain>

<decisions>
## Implementation Decisions

- Keep the shortest-path implementation inside `src/wekruit_matching/api/internal_ui.py` instead of introducing a template engine or frontend stack.
- Ship internal mode now, but define token and shell hooks so an external mode can reuse the same markup later.
- Fix accessibility at the shell layer first: page heading, landmarks, labels, focus visibility, skip link.
- Add render-level tests against fake DB results so the shell contract is verifiable without live Postgres.

</decisions>

<code_context>
## Existing Code Insights

- `src/wekruit_matching/api/internal_ui.py` contains all current HTML/CSS for jobs, stats, and pipeline pages.
- `src/wekruit_matching/api/server.py` already mounts the internal router; no API wiring changes are required.
- Audit findings identified missing `.sr-only`, missing page `h1`, inline styles, and inconsistent page hierarchy as the highest-value fixes.

</code_context>

<specifics>
## Specific Ideas

- Introduce one `page-shell` with brand lockup, current-page nav state, and summary header.
- Replace scattered inline styling with reusable classes for sections, badges, stat cards, and pagination controls.
- Define `body[data-surface="internal" | "external"]` tokens now even if only internal ships.

</specifics>

<deferred>
## Deferred Ideas

- Rich charts and advanced sorting remain in later milestones.
- Any dedicated template/component extraction beyond this file is deferred until the UI structure stabilizes.

</deferred>
