# Phase 10: Jobs Browsing UX Overhaul - Context

**Gathered:** 2026-03-31  
**Status:** Ready for execution  
**Mode:** Autonomous fallback (nested repo GSD root conflict)

<domain>
## Phase Boundary

Improve the jobs and stale listing experience so filtering, status comprehension, and pagination remain usable on narrow screens and do not rely on color alone.

</domain>

<decisions>
## Implementation Decisions

- Preserve desktop density with tables, but add a dedicated mobile card layout instead of relying on horizontal-only scroll.
- Replace shorthand processing codes like `Y/--` with explicit text badges.
- Use proper query-string encoding for pagination so filters survive navigation correctly.

</decisions>

<code_context>
## Existing Code Insights

- Jobs and stale listings share one endpoint; status drives both route context and page navigation state.
- The previous implementation already had the right data available (`status`, `sponsorship`, `enriched_at`, `embedded_at`) but exposed it with weak labels.

</code_context>
