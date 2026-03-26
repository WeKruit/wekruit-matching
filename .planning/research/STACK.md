# Stack Research

**Domain:** Python job scraping + vector matching engine
**Researched:** 2026-03-25
**Confidence:** HIGH (most choices verified against PyPI, official docs, and multiple independent sources)

---

## Recommended Stack

### Core Technologies

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Python | 3.12+ | Runtime | Spec constraint; 3.12 is stable LTS with noticeable performance gains over 3.10/3.11; 3.13 exists but 3.12 has broader library compatibility |
| PostgreSQL | 16+ | Primary database | Spec constraint; required for pgvector extension; Postgres 16 added parallel query improvements relevant to vector workloads |
| pgvector | 0.4.2 (Python lib) / extension 0.8+ | Vector similarity search inside Postgres | Eliminates a dedicated vector DB (Pinecone, Weaviate, Qdrant); for this scale (thousands of job listings, not millions), co-locating structured data + embeddings in one DB is the right call — simpler ops, one connection pool, transactional guarantees |
| psycopg (v3) | 3.x | Postgres adapter | New projects should use psycopg3; 3-5x memory efficiency over psycopg2; native async support; required by pgvector HNSW best practices (LangChain and pgvector-python both migrate away from psycopg2); psycopg2 is maintenance-only with no new features planned |
| httpx | 0.28.1 | HTTP client for GitHub raw content | Spec constraint; sync + async in one library; HTTP/2 support; Requests-style API without the legacy baggage; correct choice for a scraper that may need to parallelize fetches without committing to a fully async codebase |
| anthropic | 0.86.0 | LLM enrichment (Claude) | Spec constraint; current SDK is actively maintained (weekly releases); use for job classification, industry tagging, sponsorship detection — tasks requiring language understanding |
| openai | 2.30.0 | Embedding generation | Spec constraint; text-embedding-3-small produces 1536-dim vectors; best cost/quality ratio for semantic job matching at this scale; $0.02/1M tokens makes bulk embedding affordable |
| numpy | 1.26+ / 2.x | Vector arithmetic for multi-signal scoring | Spec constraint; correct for weighted score computation in Python; cosine similarity, dot products, normalization — all fast via numpy without pulling in scipy or sklearn |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pydantic | v2 (2.x) | Schema validation for job/user/profile models | Use for ALL data models; v2 is Rust-backed, 5-50x faster than v1; use `pydantic-settings` subpackage (separate install) for env var management — replaces python-dotenv |
| pydantic-settings | 2.x | Typed environment variable management | Always; type-safe config with validation errors that tell you what's missing; prefer over bare `python-dotenv` which silently mutates `os.environ` |
| alembic | 1.18.x | Database migration management | Always; the de facto standard for SQLAlchemy-managed Postgres schemas; use autogenerate to track schema diffs |
| SQLAlchemy | 2.x | ORM / query builder | Use for schema definitions and migrations (pairs with alembic); optional for queries — for a backend-only engine, raw psycopg3 queries are acceptable for hot paths like matching |
| tenacity | 8.x | Retry logic with exponential backoff | Required for all LLM API calls (Anthropic + OpenAI); exponential backoff with jitter is the industry standard for 429 handling; 3-5 retries with 2-30s backoff window |
| loguru | 0.7.x | Application logging | Use over stdlib logging and structlog; single import, sane defaults, structured output without ceremony; structlog is more powerful but overkill for a backend script/API |
| python-dateutil | 2.x | Date parsing for job posting timestamps | Handles ambiguous date formats from GitHub README tables (e.g., "Aug 15", "2025-08-15") without manual strptime patterns |
| mistune or mistletoe | mistune 3.x | Markdown table parsing | For parsing SimplifyJobs README tables; mistune is faster and better maintained for Python 3.12+; mistletoe is spec-compliant but heavier; both work — pick mistune |
| APScheduler | 3.x | Cron-based scheduling | For wrapping scraper + enrichment scripts in a persistent scheduler; supports cron triggers; `schedule` library is fine for simple scripts but lacks persistence across restarts |
| pytest | 8.x | Test runner for end-to-end pipeline tests | Standard; use with `pytest-asyncio` if async paths need testing |
| uv | latest | Package manager and virtual environment | Replaces pip + venv; 10-100x faster than pip; Rust-backed; single binary; correct choice for a greenfield 2026 Python project |

### Development Tools

| Tool | Purpose | Notes |
|------|---------|-------|
| uv | Dependency management, venv, Python version pinning | Use `uv init`, `uv add`, `uv run`; commit `uv.lock` for reproducible installs; replaces `requirements.txt + pip + virtualenv` |
| ruff | Linting + formatting | Replaces flake8 + black + isort in a single Rust-backed binary; fast enough to run on save |
| pyright / basedpyright | Type checking | Strict mode catches psycopg3 and pydantic v2 type errors before runtime |

---

## Installation

```bash
# Initialize project with uv
uv init wekruit-matching
cd wekruit-matching

# Core runtime dependencies
uv add psycopg[binary] pgvector sqlalchemy alembic
uv add httpx anthropic openai numpy pydantic pydantic-settings
uv add tenacity loguru python-dateutil mistune apscheduler

# Dev dependencies
uv add --dev pytest pytest-asyncio ruff pyright
```

---

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| psycopg (v3) | psycopg2 | Only if inheriting a legacy codebase already on psycopg2; no reason to start new on psycopg2 in 2026 |
| pgvector in Postgres | Pinecone / Qdrant / Weaviate | At 100K+ vectors with sub-10ms p99 latency requirements; this project will have thousands of job listings, not millions — dedicated vector DB is unnecessary complexity |
| numpy for scoring | scipy / scikit-learn | If you need pairwise similarity matrices or clustering at scale; numpy is sufficient for per-user scoring against a job corpus of this size |
| FastAPI | Flask | Flask is acceptable for lower-throughput internal APIs; FastAPI is better when callers (Discord bot, web app) need typed contracts and auto-generated OpenAPI docs |
| FastAPI | Plain Python functions / library API | Correct if the matching engine is consumed as a Python library directly (no HTTP); spec says "no frontend" and "any client can consume the matching API" — suggests HTTP API is expected, making FastAPI appropriate |
| mistune | BeautifulSoup / lxml | GitHub serves raw markdown — no HTML to parse; using an HTML parser for markdown tables is the wrong abstraction |
| APScheduler | System cron + standalone scripts | System cron is simpler and more reliable for production; APScheduler is better for development where you want scheduling embedded in the process; spec says "cron-ready scripts" which favors standalone scripts + system cron in production |
| pydantic-settings | python-dotenv | python-dotenv silently mutates os.environ and provides no type validation; pydantic-settings surfaces config errors at startup, not mid-execution |
| loguru | structlog | structlog is superior for distributed systems with structured log aggregation pipelines; loguru is correct for a standalone backend service/script |
| alembic | raw SQL migrations | Raw SQL is fine for throwaway projects; alembic autogenerate is materially faster for iterating on schema during development |
| uv | pip + virtualenv / poetry | pip lacks lock files; poetry is slower and heavier; uv is the 2025/2026 standard for new Python projects |

---

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| requests | Synchronous-only; no HTTP/2; missing modern features; httpx is a strict superset | httpx |
| aiohttp | Async-only; forces async everywhere; httpx gives you sync when you want it, async when you need it | httpx |
| psycopg2 | Maintenance-only (no new features); 3-5x worse memory efficiency; no native asyncio; pgvector-python documentation specifically recommends psycopg3 for new projects | psycopg (v3) |
| Pinecone / Qdrant / Weaviate | Operational overhead of a separate service for a corpus measured in thousands, not millions; adds latency, cost, and a second system to keep running | pgvector inside Postgres |
| LangChain | Abstracts away the exact API calls you need to control for cost optimization; for this project, direct SDK calls give you precise control over when enrichment runs and what gets cached | anthropic SDK + openai SDK directly |
| IVFFlat index (pgvector) | Requires selecting `lists` parameter upfront and rebuilds poorly as data grows; HNSW has better query latency/recall tradeoff and handles incremental inserts better | HNSW index in pgvector |
| pip + requirements.txt | No lock file, slow, no Python version management | uv |
| black + flake8 + isort separately | Three tools doing what ruff does 10-100x faster in one binary | ruff |

---

## Stack Patterns by Variant

**If running as a library (imported by Discord bot directly):**
- Drop FastAPI; expose a `match(user_profile) -> list[JobMatch]` function
- Keep everything else; psycopg3 + pgvector + pydantic models work identically
- This is viable — the matching engine is pure Python, FastAPI is only needed if HTTP is required

**If running as an HTTP API (Discord bot calls over HTTP):**
- Add FastAPI + uvicorn
- Pydantic models double as FastAPI request/response schemas automatically
- Use `uvicorn --workers 1` for a single-worker process unless concurrency becomes a concern

**If embedding corpus grows to 100K+ jobs:**
- Consider dimensionality reduction (1536 → 768 via PCA) — halves HNSW memory, doubles query throughput, retains ~97% recall
- At this scale (intern + new grad listings from SimplifyJobs), you will not reach 100K; this is not a near-term concern

**If cron scheduling is needed in production:**
- Prefer system cron (crontab) + standalone Python scripts over APScheduler in-process
- APScheduler is fine for local development and Docker-based deployments without cron access
- The spec's "cron-ready scraper and enrichment scripts" language implies standalone scripts are the target

---

## Version Compatibility

| Package | Compatible With | Notes |
|---------|-----------------|-------|
| pgvector 0.4.2 (Python) | psycopg 3.x, psycopg2, asyncpg, SQLAlchemy 2.x | Do NOT mix psycopg2 connection strings with psycopg3 style (`postgresql+psycopg://` not `postgresql+psycopg2://`) |
| pydantic v2 | pydantic-settings 2.x | pydantic-settings is a separate package in v2; `pip install pydantic-settings` separately from `pydantic` |
| SQLAlchemy 2.x | alembic 1.18.x | SQLAlchemy 2.x required for alembic 1.18+; do not pin SQLAlchemy 1.x |
| httpx 0.28.1 | Python 3.8+ | Stable release; 1.0 dev versions exist but are pre-release; use 0.28.1 |
| anthropic 0.86.0 | Python 3.9+ | Active weekly releases; pin to minor (e.g., `anthropic>=0.86,<1.0`) |
| openai 2.30.0 | Python 3.9+ | Pin to minor; openai has had breaking changes between major versions |

---

## Spec Validation

The spec's technology choices are validated. No changes recommended for the core spec:

| Spec Choice | Verdict | Notes |
|-------------|---------|-------|
| Python 3.12+ | CONFIRMED | Correct; stable, widely supported, good performance |
| Postgres + pgvector | CONFIRMED | Correct for this scale; right call to avoid a dedicated vector DB |
| httpx | CONFIRMED | Correct choice; sync+async, HTTP/2, actively maintained |
| Anthropic API for enrichment | CONFIRMED | Correct; Claude is strong at structured classification tasks |
| text-embedding-3-small | CONFIRMED | Correct; 1536 dims, best cost/quality for semantic job matching |
| numpy for vector math | CONFIRMED | Correct; sufficient for per-user weighted scoring; no need for scipy overhead |
| Cron-based scheduling | CONFIRMED | Correct framing; implement as standalone scripts callable by system cron |

One addition not in spec but required: **psycopg3** (psycopg package, not psycopg2) as the Postgres adapter. The spec mentions Postgres but not which adapter. Psycopg3 is the correct 2026 choice.

---

## Sources

- PyPI: pgvector 0.4.2 — version confirmed December 5, 2025
- PyPI: anthropic 0.86.0 — version confirmed March 18, 2026
- PyPI: openai 2.30.0 — version confirmed March 25, 2026
- PyPI: httpx 0.28.1 — version confirmed December 6, 2024
- Alembic docs (alembic.sqlalchemy.org) — version 1.18.4 confirmed
- psycopg.org/psycopg3 — "if you are starting a new project, you should probably start from psycopg3"
- github.com/pgvector/pgvector-python — confirmed psycopg3 + asyncpg + SQLAlchemy 2.x compatibility
- OpenAI platform docs — text-embedding-3-small: 1536 dims, 8192 token limit, cl100k_base encoding
- Crunchydata HNSW blog — HNSW preferred over IVFFlat for recall/latency tradeoff
- Multiple sources (BetterStack, Speakeasy, Oxylabs) — httpx vs requests vs aiohttp comparison; MEDIUM confidence on performance claims, HIGH on feature set
- Multiple sources (TigerData benchmark, psycopg.org) — psycopg3 vs psycopg2 performance; HIGH confidence on direction
- Multiple sources (Medium, Upsun, DataCamp) — uv vs pip vs poetry 2025; HIGH confidence on direction

---

*Stack research for: Python job scraping + vector matching engine (WeKruit Matching)*
*Researched: 2026-03-25*
