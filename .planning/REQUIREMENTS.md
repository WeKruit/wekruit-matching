# Requirements: WeKruit Matching Engine

**Defined:** 2026-03-31
**Core Value:** People monitoring the WeKruit job corpus can immediately understand what jobs exist, what changed, and whether the pipeline is healthy.

## v1 Requirements

### Console Foundation

- [ ] **CONS-01**: User can move between Jobs, Stale, Stats, and Pipeline from a shared page shell with a clear current-page title and context.
- [ ] **CONS-02**: User sees a consistent WeKruit visual system across all internal pages instead of page-specific hard-coded styling.
- [ ] **CONS-03**: UI structure supports both internal and future external surface modes without requiring a page-by-page rewrite.

### Accessibility

- [ ] **A11Y-01**: Keyboard user can identify and operate primary navigation, filters, job links, and pagination controls with visible focus states.
- [ ] **A11Y-02**: Each page exposes valid heading hierarchy, landmarks, and labeled form controls.
- [ ] **A11Y-03**: Status information is communicated with text and structure, not color alone.

### Jobs Browsing

- [ ] **JOBS-01**: User can filter jobs by status, source, industry, and text search from one coherent filter region.
- [ ] **JOBS-02**: User can browse jobs and stale listings on narrow viewports without relying on horizontal-scroll-only access to core information.
- [ ] **JOBS-03**: User can understand job freshness and processing state at a glance, including active/inactive state, sponsorship, and enrichment/embedding status.
- [ ] **JOBS-04**: User can paginate through filtered job results without losing filter context.

### Stats and Pipeline

- [ ] **STAT-01**: User can understand inventory health from the stats page at a glance through clear summary hierarchy.
- [ ] **STAT-02**: User can scan source, industry, and recent intake sections on desktop and narrow viewports with consistent layout rules.
- [ ] **PIPE-01**: User can understand pending enrichment, pending embedding, and latest pipeline activity from the pipeline page without reading raw operational jargon.
- [ ] **PIPE-02**: Pipeline timestamps, labels, and explanatory copy are understandable to future customer-facing users, not only internal operators.

### Responsive Quality

- [ ] **RESP-01**: Primary interactive controls meet touch-friendly target sizing expectations across supported viewports.
- [ ] **RESP-02**: Shared layout spacing and grouping remain usable on narrow screens across Jobs, Stats, and Pipeline.

## v2 Requirements

### External Surface

- **EXT-01**: External mode presents the jobs console with customer-facing copy, framing, and chrome distinct from internal mode.
- **EXT-02**: User can switch or route between internal and external console presentations without duplicating page logic.

### Advanced Browsing

- **BROW-01**: User can sort jobs by freshness, company, or processing completeness.
- **BROW-02**: User can open richer job-detail views without leaving the console.

### Visualization

- **VIZ-01**: Stats page includes chart-based visual summaries for intake and source composition.
- **VIZ-02**: Pipeline page exposes trend and latency indicators beyond latest timestamps.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Matching logic changes | This milestone is explicitly UI-only |
| New recommender or ranking work | Owned outside this UI effort |
| VALET / desktop / onboarding / billing changes | Different products and repos |
| Authenticated customer accounts | Not required to establish the jobs-console UI foundation |
| New data-source ingestion | Not necessary for the UI restructure |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| CONS-01 | Phase 9 — Console Shell & Design Tokens | Pending |
| CONS-02 | Phase 9 — Console Shell & Design Tokens | Pending |
| CONS-03 | Phase 11 — Customer-Facing Readiness & Final Polish | Pending |
| A11Y-01 | Phase 9 — Console Shell & Design Tokens | Pending |
| A11Y-02 | Phase 9 — Console Shell & Design Tokens | Pending |
| A11Y-03 | Phase 10 — Jobs Browsing UX Overhaul | Pending |
| JOBS-01 | Phase 10 — Jobs Browsing UX Overhaul | Pending |
| JOBS-02 | Phase 10 — Jobs Browsing UX Overhaul | Pending |
| JOBS-03 | Phase 10 — Jobs Browsing UX Overhaul | Pending |
| JOBS-04 | Phase 10 — Jobs Browsing UX Overhaul | Pending |
| STAT-01 | Phase 11 — Customer-Facing Readiness & Final Polish | Pending |
| STAT-02 | Phase 11 — Customer-Facing Readiness & Final Polish | Pending |
| PIPE-01 | Phase 11 — Customer-Facing Readiness & Final Polish | Pending |
| PIPE-02 | Phase 11 — Customer-Facing Readiness & Final Polish | Pending |
| RESP-01 | Phase 10 — Jobs Browsing UX Overhaul | Pending |
| RESP-02 | Phase 11 — Customer-Facing Readiness & Final Polish | Pending |

**Coverage:**
- v1 requirements: 16 total
- Mapped to phases: 16
- Unmapped: 0 ✓

---
*Requirements defined: 2026-03-31*
*Last updated: 2026-03-31 after roadmap creation*
