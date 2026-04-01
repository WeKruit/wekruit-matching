# WeKruit Matching Engine

## What This Is

A Python service that scrapes, enriches, stores, and serves intern and new-grad job data for WeKruit. It already powers the job corpus and operational APIs, and now includes an internal web UI for browsing jobs, inspecting stale listings, and monitoring pipeline health. This milestone evolves that UI into a structured jobs console that is strong enough to support both internal operators and a future customer-facing surface.

## Core Value

People monitoring the WeKruit job corpus can immediately understand what jobs exist, what changed, and whether the pipeline is healthy.

## Current Milestone: v1.1 Internal UI Foundation — Dual-Surface Jobs Console

**Goal:** Turn the current internal HTML pages into a coherent WeKruit jobs console with strong hierarchy, accessibility, responsive behavior, and a foundation that can support both internal and future customer-facing modes.

**Target features:**
- Shared page shell across Jobs, Stale, Stats, and Pipeline
- Tokenized visual system aligned to WeKruit brand instead of scattered hard-coded styles
- Accessibility fixes for headings, labels, focus states, and semantic status communication
- Responsive layouts that work on narrow viewports without relying on table-only horizontal scrolling
- Information architecture that reads as a product surface, not a temporary internal tool
- Structural support for internal and external surface modes under one design system

## Requirements

### Validated

- ✓ Daily scrape, enrich, and embed pipeline populates the jobs corpus — v1.0 Phases 1-8
- ✓ Jobs lifecycle supports active and inactive/stale listings — v1.0 Phase 2
- ✓ Matching and stats endpoints expose job inventory data to consumers — v1.0 shipped backend
- ✓ Internal HTML pages exist for jobs, stale jobs, stats, and pipeline health — baseline shipped before v1.1

### Active

- [ ] Shared jobs-console shell across Jobs, Stale, Stats, and Pipeline
- [ ] WeKruit-aligned tokenized visual system for internal UI
- [ ] Keyboard-accessible navigation, filters, pagination, and page structure
- [ ] Responsive jobs browsing experience for desktop and narrow viewports
- [ ] Clear status language for freshness, sponsorship, enrichment, and embedding state
- [ ] Stats and pipeline pages with customer-facing-ready hierarchy and clarity
- [ ] Structural support for separate internal and external surface modes without rebuilding the UI

### Out of Scope

- Matching logic changes — this milestone is UI-only
- New ranking signals or recommender behavior — separate product already owns matching evolution
- VALET, desktop, onboarding, or billing work — outside this repo and milestone
- Full authentication / customer account model for the jobs console — not required for the current UI overhaul
- New data sources or pipeline architecture changes — defer unless the UI work uncovers a blocking issue

## Context

- **Current UI implementation:** All internal pages are rendered from `src/wekruit_matching/api/internal_ui.py` with one large inline CSS string and server-rendered HTML responses.
- **Current pages:** `/internal/jobs`, `/internal/jobs?status=inactive`, `/internal/stats`, `/internal/pipeline`
- **Design references:** `wekruit.com`, `WeKruit/wekruit-outbound` `DESIGN.md`, and VALET's established espresso / ivory / amber brand system
- **Audit baseline:** The current UI scored poorly in accessibility, responsive design, and theming due to missing semantics, fixed table-oriented layouts, and hard-coded styles
- **Surface strategy:** One WeKruit design system with two modes — denser operator console for internal use and lighter customer-facing presentation later

## Constraints

- **Tech stack**: FastAPI HTML responses in Python — UI work must fit the existing server-rendered architecture
- **Scope**: UI-only milestone — avoid mixing in matching-engine logic changes
- **Brand**: Must align to WeKruit design system references — no generic blue SaaS styling
- **Accessibility**: Customer-facing direction requires WCAG AA-minded structure and interaction patterns
- **Responsiveness**: Core workflows must remain usable on narrow viewports, not only on desktop admin screens

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Evolve the internal UI instead of replacing it with a separate app | Existing pages already provide the right operational data and routes; the problem is presentation, not product fit | — Pending |
| Build one design system with internal and external modes | The UI must serve operators now but be strong enough for customer-facing use later | — Pending |
| Keep light mode as the primary experience | Matches WeKruit brand guidance and current sibling products | — Pending |
| Prioritize shell, semantics, and responsive structure before visual polish | The audit shows systemic UI debt; polish without structure would be fragile | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd:transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd:complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-03-31 after milestone v1.1 initialization*
