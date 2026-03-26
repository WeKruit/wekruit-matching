# WeKruit Matching Engine

Backend pipeline: scrapes intern and new-grad job listings from SimplifyJobs GitHub repos,
enriches them with LLM-derived metadata and semantic embeddings, and returns ranked job
matches against a user profile. No HTTP server — import and call directly.

## Prerequisites

- Python 3.12+
- PostgreSQL 16+ with pgvector extension installed
- [uv](https://docs.astral.sh/uv/) package manager

## Setup

### 1. Install dependencies

    uv sync

### 2. Configure environment

    cp .env.example .env
    # Edit .env and fill in all required values (see .env.example for details)

### 3. Run database migrations

    uv run alembic upgrade head

## Running the Pipeline

### One-shot (development)

Run each step manually in order:

    # 1. Scrape job listings from SimplifyJobs
    uv run python -m wekruit_matching.scraper.run

    # 2. Enrich jobs with LLM metadata (industry, skills, sponsorship)
    uv run python -m wekruit_matching.enrichment.run

    # 3. Generate semantic embeddings
    uv run python -m wekruit_matching.embedding.run

### End-to-end test

    uv run python scripts/e2e_test.py

Runs the full pipeline (scrape -> enrich -> embed -> match -> feedback) and prints
ranked results for a test profile.

## Scheduled Runs (Production)

Install cron jobs (scraper at 6 AM ET, enrichment+embedding at 6:30 AM ET):

    bash scripts/install_cron.sh

Logs are written to `/tmp/wekruit_scraper.log` and `/tmp/wekruit_enrichment.log`.

**Note:** Cron runs in the system timezone. If your server is not set to
`America/New_York`, adjust the schedule in `scripts/install_cron.sh` accordingly.

## Library Usage

```python
from wekruit_matching import get_matches, record_feedback
from wekruit_matching.models.user_profile import UserProfile, JobType

profile = UserProfile(
    user_id="alice",
    skills=["Python", "machine learning", "SQL"],
    preferred_job_type=JobType.INTERN,
    preferred_locations=["Remote", "SF"],
    requires_sponsorship=False,
)

matches = get_matches(profile, top_n=10)
for job in matches:
    print(f"{job['role_title']} @ {job['company_name']}  score={job['score']:.3f}")

# Record feedback
record_feedback("alice", matches[0]["job_id"], "like")
```

## Running Tests

    uv run pytest

## Environment Variables

See `.env.example` for full documentation of all required variables.

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | Yes | PostgreSQL connection string (psycopg3 format) |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for LLM enrichment |
| `OPENAI_API_KEY` | Yes | OpenAI API key for embedding generation |
| `GITHUB_TOKEN` | Yes | GitHub PAT for scraping SimplifyJobs repos |
| `LOG_LEVEL` | No | Log verbosity (default: INFO) |
