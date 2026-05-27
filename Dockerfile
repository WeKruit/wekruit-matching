# syntax=docker/dockerfile:1.7
#
# wekruit-matching — portable scrape/enrich pipeline runner.
#
# Build:   docker build -t wekruit-matching:latest .
# Run:     docker compose up -d
# One-off: docker compose run --rm app uv run python -m wekruit_matching.pipeline.daily
#
# Why this exists
# ---------------
# Until 2026-05-27 the pipeline was pinned to a single Mac mini via launchd. When
# the host was unreachable for five days the scrape stopped and nothing alerted.
# This image bundles the full runtime so any Docker-capable host (Adam's laptop,
# a Linux VPS, Cloud Run jobs) can take over without touching the macmini.
FROM python:3.12-slim-bookworm AS base

# Python defaults — unbuffered stdout for `docker logs`, no .pyc cruft.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# System deps:
#   - build-essential + libpq-dev are needed by psycopg[binary] wheels on
#     unusual archs; the official wheels usually skip the compile step.
#   - curl for the uv installer.
#   - ca-certificates so outbound TLS to api.anthropic.com etc. works.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Install uv (the project's package manager) at a pinned version. We keep it
# in /usr/local/bin so it's on PATH for every layer.
COPY --from=ghcr.io/astral-sh/uv:0.5.18 /uv /usr/local/bin/uv

WORKDIR /app

# Cache layer: install dependencies before copying source so a code change
# doesn't bust the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

# Now bring in the rest of the source.
COPY . .

# Install the project itself (so `python -m wekruit_matching.pipeline.daily`
# resolves), then strip uv's cache to keep the image small.
RUN uv sync --frozen \
    && rm -rf /root/.cache/uv

# Entrypoint waits for the database, runs `alembic upgrade head`, then execs
# whatever command the caller passed. Skipped when `SKIP_ALEMBIC=1`.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh
ENTRYPOINT ["entrypoint.sh"]

# Default command: launch the FastAPI server. `docker compose run` overrides
# this for one-shot scrape invocations.
CMD ["uv", "run", "uvicorn", "wekruit_matching.api.server:app", \
     "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
