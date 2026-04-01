---
phase: 09-console-shell-design-tokens
plan: "01"
subsystem: internal-ui
tags: [shell, tokens, accessibility, navigation]
---

# Phase 09 Plan 01 Summary

Implemented one shared SSR shell for the internal console in `src/wekruit_matching/api/internal_ui.py`.

## What Changed

- Added one reusable WeKruit shell with brand lockup, current-page navigation, skip link, page hero, and summary strip.
- Replaced scattered hard-coded/inline styles with a shared token layer and reusable section, badge, metric-card, and pagination classes.
- Fixed core semantic issues: page-level `h1`, labeled controls, landmarks, and shared focus treatment.
- Added `tests/test_internal_ui.py` to lock shell behavior through render-level assertions.

## Verification

- `uv run ruff check src/wekruit_matching/api/internal_ui.py tests/test_internal_ui.py`
- `uv run pytest tests/test_internal_ui.py -q`
- `uv run python -m py_compile src/wekruit_matching/api/internal_ui.py tests/test_internal_ui.py`
