## Design Context

### Users

This surface serves two audiences under one shared system:

- Internal operators who need to browse jobs, inspect stale/inactive listings, monitor intake, and understand pipeline health quickly.
- External customers who will later use a customer-facing version of the same surface to inspect job inventory and system status without seeing a raw internal console.

The job to be done is operational clarity: help people understand what jobs exist, what changed, and whether the pipeline is healthy, without making them parse a noisy dashboard.

The interface should reduce ambiguity, surface status clearly, and make dense information feel composed instead of cluttered.

### Brand Personality

**Calm. Trustworthy. Restrained.**

This repo should align to the broader WeKruit brand system established in `wekruit-outbound/DESIGN.md` and reinforced by VALET:

- high-judgment
- restrained
- trustworthy
- human
- premium without ornament

Voice and presentation should feel direct, clear, and serious, not cold, robotic, or generic enterprise SaaS.

### Aesthetic Direction

This UI should use one shared WeKruit design language with two surface modes:

- **Internal mode**: operator-console oriented, denser, more structured, optimized for scanning and control.
- **External mode**: customer-facing, lighter, more editorial, with reduced chrome and a more composed presentation layer.

Both modes must remain in the same family identity:

- warm ivory surfaces
- espresso ink tones
- amber as the primary accent
- Halant for display moments where authority matters
- Geist for UI, labels, tables, and dense operational content

Primary references:

- `wekruit.com`
- `WeKruit/wekruit-outbound` `DESIGN.md`

Anti-direction:

- no generic blue SaaS dashboard styling
- no neon AI startup aesthetic
- no developer-tool / terminal visual language
- no cluttered card stacking or over-explained UI

Light mode is the primary experience. Any future dark mode must remain secondary to a strong light-mode baseline.

### Design Principles

1. **One brand, two modes** — Internal and customer-facing surfaces may differ in density and chrome, but they must share the same typography, palette, spacing rhythm, and overall judgment.

2. **Operational clarity first** — Jobs, status, freshness, and pipeline health must be readable at a glance. Filters, summaries, and page hierarchy should reduce scanning cost, not increase it.

3. **Restraint over dashboard noise** — Use fewer colors, fewer competing emphasis points, and fewer visual containers. Structure should come from spacing, typography, and grouping before decoration.

4. **Warmth without softness** — The UI should feel human and premium through warm neutrals and measured accents, but still precise and competent enough for operational work.

5. **Customer-facing readiness** — Even when building internal pages, avoid internal-only ugliness. Layouts, empty states, navigation, and data presentation should be strong enough to evolve into external-facing product surfaces later.
