# Milestones

## v1.1 Internal UI Foundation

**Status:** Shipped 2026-03-31  
**Phases:** 9-11  
**Plans:** 3  
**Audit:** [v1.1-MILESTONE-AUDIT.md](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/v1.1-MILESTONE-AUDIT.md)

### Delivered

- One shared WeKruit shell for Jobs, Stale, Stats, and Pipeline
- Tokenized styling foundation instead of page-specific inline styling
- Accessible page structure with page headings, labels, focus treatment, and text-based status
- Responsive jobs browsing with a mobile card layout instead of table-only horizontal scrolling
- Stats and pipeline pages reframed into calmer, customer-facing-ready product surfaces

### Artifacts

- [v1.1-ROADMAP.md](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/milestones/v1.1-ROADMAP.md)
- [v1.1-REQUIREMENTS.md](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/milestones/v1.1-REQUIREMENTS.md)
- [v1.1-phases](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/milestones/v1.1-phases)

## v1.2 Job Data Pipeline

**Status:** Shipped 2026-03-31  
**Phases:** 14-18  
**Plans:** 5  
**Audit:** [v1.2-MILESTONE-AUDIT.md](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/v1.2-MILESTONE-AUDIT.md)

### Delivered

- JD fetch-tracking schema and deterministic ATS URL routing
- Free ATS parsers for Greenhouse, Lever, and Ashby with normalized text and quality scoring
- Workday CXS discovery plus Firecrawl scrape/extract/search fallback with async timeout protection
- Stage 2b `run_jd_enrichment.py` integrated into `daily.py`
- JD-aware metadata classification, pipeline page observability, and richer completion digest
- Live-DB gate over the latest 1K jobs for Greenhouse, Lever, and Ashby coverage

## v2.0 Platform Unification

**Status:** Implementation complete, live cutover pending  
**Phases:** 19-23  
**Plans:** 5  
**Delib:** [v2.0-DELIB.md](/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.planning/v2.0-DELIB.md)

### Intended Outcome

- Firebase Core Service becomes the central hub for customer-facing APIs
- VALET users sync into Firestore without VALET code changes
- Mac Mini job corpus syncs into `matching-jobs`
- Firestore-backed matching replaces the Python-only serving path
- Job browse/detail API lives on Cloud Functions with declared index contracts
