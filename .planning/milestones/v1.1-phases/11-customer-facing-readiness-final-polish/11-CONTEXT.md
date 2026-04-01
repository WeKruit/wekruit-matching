# Phase 11: Customer-Facing Readiness & Final Polish - Context

**Gathered:** 2026-03-31  
**Status:** Ready for execution  
**Mode:** Autonomous fallback (nested repo GSD root conflict)

<domain>
## Phase Boundary

Bring stats and pipeline up to the same visual and structural standard as jobs, and ensure the whole console is ready to evolve into a customer-facing surface without a rewrite.

</domain>

<decisions>
## Implementation Decisions

- Use the same hero, section, and metric-card primitives across all pages so no page falls back to ad hoc layout.
- Rewrite pipeline language from raw internal shorthand into plain product terms.
- Treat internal/external split as a surface-mode concern, not a separate markup tree.

</decisions>

<code_context>
## Existing Code Insights

- Stats and pipeline pages already had the necessary query data but lacked consistent hierarchy and explanatory framing.
- The new shell primitives from Phase 9 are sufficient to finish this phase without new infrastructure.

</code_context>
