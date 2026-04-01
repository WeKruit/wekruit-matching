# Roadmap: WeKruit Matching Engine

## Overview

Milestone v1.1 focuses on the internal jobs console rather than the matching engine itself. The goal is to turn the existing `/internal/jobs`, `/internal/stats`, and `/internal/pipeline` pages into a coherent WeKruit UI foundation: shared shell, accessible structure, responsive jobs browsing, and a brand-aligned information architecture that can later support both internal and customer-facing surface modes. This roadmap starts after the completed backend milestone, so phase numbering continues from Phase 8.

## Phases

**Phase Numbering:**
- Integer phases (9, 10, 11): Planned milestone work
- Decimal phases (9.1, 9.2): Urgent insertions if needed

- [ ] **Phase 9: Console Shell & Design Tokens** - Shared shell, page hierarchy, semantic fixes, and reusable visual foundations for all internal pages
- [ ] **Phase 10: Jobs Browsing UX Overhaul** - Responsive jobs/stale views, better status communication, and touch-friendly filtering/pagination
- [ ] **Phase 11: Customer-Facing Readiness & Final Polish** - Stats/pipeline hierarchy, dual-surface readiness, and cross-page consistency polish

## Phase Details

### Phase 9: Console Shell & Design Tokens
**Goal**: All internal pages share one WeKruit-aligned shell with clear titles, navigation, accessible structure, and reusable visual primitives instead of scattered hard-coded styles
**Depends on**: Phase 8
**Requirements**: CONS-01, CONS-02, A11Y-01, A11Y-02
**Success Criteria** (what must be TRUE):
  1. Jobs, Stale, Stats, and Pipeline all render inside one shared page shell with a current-page title, contextual summary region, and consistent navigation structure
  2. Keyboard users can see and follow focus across nav, filters, links, and pagination; form controls have visible labels and page headings are semantically correct
  3. Shared colors, spacing, and status treatments are defined from one internal token layer rather than repeated inline styles and one-off values
  4. The shell and primitives are structured so later internal/external mode differences can be applied without cloning page markup
**Plans**: TBD
**UI hint**: yes

### Phase 10: Jobs Browsing UX Overhaul
**Goal**: Users can browse active and stale jobs from a clear, responsive interface that preserves operational density without collapsing into a desktop-only table
**Depends on**: Phase 9
**Requirements**: A11Y-03, JOBS-01, JOBS-02, JOBS-03, JOBS-04, RESP-01
**Success Criteria** (what must be TRUE):
  1. Jobs and Stale pages expose a coherent filter region for status, source, industry, and text search that works with keyboard and touch input
  2. On narrow viewports, the jobs experience remains usable without depending on horizontal-scroll-only access to core job information
  3. Each job row or card clearly communicates freshness and processing state, including active/inactive status, sponsorship, and enrichment/embedding progress, with text and not color alone
  4. Pagination preserves the user's active filters and remains touch-friendly across supported viewport sizes
**Plans**: TBD
**UI hint**: yes

### Phase 11: Customer-Facing Readiness & Final Polish
**Goal**: Stats and Pipeline read as calm, product-quality informational pages and the whole console is structurally ready for later customer-facing mode work
**Depends on**: Phase 10
**Requirements**: CONS-03, STAT-01, STAT-02, PIPE-01, PIPE-02, RESP-02
**Success Criteria** (what must be TRUE):
  1. Stats page communicates headline inventory metrics first, then source, industry, and intake details in a consistent hierarchy on desktop and narrow viewports
  2. Pipeline page communicates pending work and recent activity in language that future customer-facing users can understand, not only internal operators
  3. Shared layout, spacing, and section rules are consistent across Jobs, Stats, and Pipeline, with no page reverting to ad hoc one-off styling
  4. The console has explicit structural support for internal and external surface modes, even if only internal mode is shipped first
**Plans**: TBD
**UI hint**: yes

## Progress

**Execution Order:**
Completed backend milestone: 1 → 8
Current UI milestone: 9 → 10 → 11

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Foundation | 2/2 | Complete | 2026-03-26 |
| 2. Scraper | 3/3 | Complete | 2026-03-26 |
| 3. LLM Enrichment | 2/2 | Complete | 2026-03-26 |
| 4. Embeddings | 2/2 | Complete | 2026-03-26 |
| 5. Hard Filters | 1/1 | Complete | 2026-03-26 |
| 6. Scoring Engine | 2/2 | Complete | 2026-03-26 |
| 7. Feedback Loop | 1/1 | Complete | 2026-03-26 |
| 8. Integration & Operations | 2/2 | Complete | 2026-03-26 |
| 9. Console Shell & Design Tokens | 0/TBD | Not started | - |
| 10. Jobs Browsing UX Overhaul | 0/TBD | Not started | - |
| 11. Customer-Facing Readiness & Final Polish | 0/TBD | Not started | - |
