---
phase: 01-foundation
plan: "01"
subsystem: infrastructure
tags: [uv, pydantic, pydantic-settings, python, pyproject]

# Dependency graph
requires: []
provides:
  - "wekruit_matching Python package importable via uv run"
  - "Pydantic v2 Job, UserProfile, Feedback data models with validation"
  - "pydantic-settings Settings class reading from .env with fail-fast validation"
  - "pyproject.toml with all runtime + dev dependencies declared and locked in uv.lock"
  - ".env.example documenting all 4 required env vars"
affects: [01-02, phase-2-scraper, phase-3-enrichment, phase-4-embeddings, phase-5-filters, phase-6-scoring]

# Tech tracking
tech-stack:
  added:
    - "uv (package manager + venv)"
    - "pydantic v2 (2.12.5)"
    - "pydantic-settings v2 (2.13.1)"
    - "pytest + pytest-asyncio (test runner)"
    - "ruff (linting + formatting)"
    - "psycopg3, pgvector, sqlalchemy, alembic, httpx, anthropic, openai, numpy, tenacity, loguru, python-dateutil, mistune"
  patterns:
    - "src layout: src/wekruit_matching/ with hatchling build backend"
    - "pydantic-settings BaseSettings with env_file=.env and extra=ignore"
    - "lru_cache singleton for get_settings()"
    - "field_validator on content_hash enforcing SHA-256 hex format"
    - "timezone-aware datetime.now(UTC) instead of deprecated utcnow()"
    - "TDD: RED commit (tests fail) then GREEN commit (implementation)"

key-files:
  created:
    - "pyproject.toml"
    - "uv.lock"
    - ".python-version"
    - ".env.example"
    - ".gitignore"
    - "src/wekruit_matching/__init__.py"
    - "src/wekruit_matching/config.py"
    - "src/wekruit_matching/models/__init__.py"
    - "src/wekruit_matching/models/job.py"
    - "src/wekruit_matching/models/user_profile.py"
    - "src/wekruit_matching/models/feedback.py"
    - "tests/__init__.py"
    - "tests/test_config.py"
    - "tests/test_models.py"
  modified: []

key-decisions:
  - "Use uv for package management (not pip/poetry) — 10-100x faster, proper lock file"
  - "Use pydantic-settings for env config (not python-dotenv) — type-safe, fail-fast on missing vars"
  - "Use _env_file=None in tests to isolate from project .env file"
  - "Use datetime.now(UTC) over deprecated datetime.utcnow() throughout models"
  - "Negated .env.example in .gitignore to override global ~/.gitignore_global's .env.* rule"

patterns-established:
  - "Config pattern: Settings with lru_cache get_settings() — callers import get_settings(), not Settings()"
  - "Model pattern: Pydantic v2 BaseModel with field_validator for custom format enforcement"
  - "Test pattern: monkeypatch env vars + _env_file=None for Settings test isolation"

requirements-completed: [FOUND-01, FOUND-05, FOUND-08]

# Metrics
duration: 4min
completed: 2026-03-26
---

# Phase 01 Plan 01: Project Scaffold and Data Models Summary

**uv project initialized with psycopg3/pgvector/pydantic v2 stack; Job, UserProfile, and Feedback models defined with full validation; pydantic-settings config layer reads .env and raises on missing required vars.**

## Performance

- **Duration:** 4 minutes
- **Started:** 2026-03-26T00:50:50Z
- **Completed:** 2026-03-26T00:54:49Z
- **Tasks:** 2 of 2
- **Files modified:** 14

## Accomplishments

- Scaffolded wekruit-matching uv project with all 14 runtime + dev dependencies resolved and locked (34 packages in uv.lock)
- Defined all three core Pydantic v2 data models (Job, UserProfile, Feedback) with typed fields, enums, and validators — these are the contracts every subsequent phase imports from
- Implemented pydantic-settings configuration layer that reads from `.env`, validates types, and raises `ValidationError` with field names on missing required vars

## Task Commits

Each task was committed atomically:

1. **Task 1: Initialize uv project and declare all dependencies** - `6372112` (chore)
2. **Task 2: TDD RED — failing tests for models and config** - `dfac5b0` (test)
3. **Task 2: TDD GREEN — pydantic v2 models and config implementation** - `fe376d5` (feat)

## Files Created/Modified

- `pyproject.toml` — Project metadata, all runtime + dev dependencies, ruff/pytest/pyright config
- `uv.lock` — Locked dependency graph (34 packages)
- `.python-version` — Pins Python 3.12
- `.env.example` — Documents DATABASE_URL, ANTHROPIC_API_KEY, OPENAI_API_KEY, GITHUB_TOKEN
- `.gitignore` — Excludes .env, .venv, caches, build artifacts; negates .env.example
- `src/wekruit_matching/__init__.py` — Package entry point with __version__ = "0.1.0"
- `src/wekruit_matching/config.py` — Settings(BaseSettings) with get_settings() singleton
- `src/wekruit_matching/models/__init__.py` — Re-exports Job, UserProfile, Feedback and enums
- `src/wekruit_matching/models/job.py` — Job model with JobStatus enum and content_hash validator
- `src/wekruit_matching/models/user_profile.py` — UserProfile with JobType/CompanySizePreference enums
- `src/wekruit_matching/models/feedback.py` — Feedback with ReactionType enum
- `tests/test_config.py` — 3 tests: loads from env, raises on missing DATABASE_URL, raises on missing ANTHROPIC_API_KEY
- `tests/test_models.py` — 7 tests: Job/UserProfile/Feedback validation including content_hash format

## Decisions Made

- Used `_env_file=None` in config tests to ensure test isolation from the project's `.env` file — prevents `LOG_LEVEL=DEBUG` in `.env` from polluting default-value assertions
- Negated `.env.example` in local `.gitignore` to override the global `~/.gitignore_global`'s `.env.*` pattern that was preventing `.env.example` from being staged

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed test_settings_loads_from_env test isolation**
- **Found during:** Task 2 (TDD GREEN run)
- **Issue:** Test asserted `log_level == "INFO"` (default value) but project's `.env` file has `LOG_LEVEL=DEBUG`. `Settings()` without `_env_file=None` reads the `.env` file, overriding the default.
- **Fix:** Changed test to call `Settings(_env_file=None)` to isolate from `.env` and test env-var-only behavior
- **Files modified:** `tests/test_config.py`
- **Verification:** All 10 tests pass after fix
- **Committed in:** `fe376d5`

**2. [Rule 2 - Missing critical functionality] Fixed deprecated datetime.utcnow() usage**
- **Found during:** Task 2 (GREEN test run — 8 DeprecationWarnings)
- **Issue:** `datetime.utcnow()` is deprecated in Python 3.12+ and scheduled for removal; produces naive datetimes which are ambiguous
- **Fix:** Added `_utcnow()` helper using `datetime.now(timezone.utc)` in both `job.py` and `feedback.py`; replaced all `default_factory=datetime.utcnow` usages
- **Files modified:** `src/wekruit_matching/models/job.py`, `src/wekruit_matching/models/feedback.py`
- **Verification:** Test run produces 0 warnings after fix
- **Committed in:** `fe376d5`

**3. [Rule 3 - Blocking] Fixed .gitignore negation for .env.example**
- **Found during:** Task 1 (git add attempt)
- **Issue:** Global `~/.gitignore_global` has `.env.*` pattern which blocks staging `.env.example`
- **Fix:** Added `!.env.example` negation to local `.gitignore` to override the global rule
- **Files modified:** `.gitignore`
- **Verification:** `git add .env.example` succeeds after fix
- **Committed in:** `6372112`

## Known Stubs

None — no UI rendering, no hardcoded empty values flowing to callers. Models return validated data or raise. Config raises on missing vars.

## Self-Check: PASSED
