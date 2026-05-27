#!/usr/bin/env bash
# Container entrypoint:
#   1. Wait for Postgres to accept connections (loop with a hard deadline).
#   2. Run `alembic upgrade head` so schema is current.
#   3. Exec whatever command was passed to `docker run` / `docker compose run`.
#
# Skip alembic with `SKIP_ALEMBIC=1` (useful for one-off shells).
set -euo pipefail

WAIT_DEADLINE="${DB_WAIT_DEADLINE_SECONDS:-90}"
START_TS="$(date +%s)"

# DATABASE_URL is required for every command path that hits the pipeline. The
# FastAPI server *could* technically boot without it, but every scrape stage
# expects it, so fail loud rather than silently degrade.
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "[entrypoint] DATABASE_URL is empty — refusing to start" >&2
  exit 1
fi

# Parse host + port out of the connection string. Supports the
# psycopg-style URL ("postgresql+psycopg://user:pw@host:5432/db") that
# wekruit_matching uses everywhere.
parse_host_port() {
  local url="$1"
  # Strip the scheme + creds, then split on `/` to drop the database name.
  local hostport="${url#*@}"
  hostport="${hostport%%/*}"
  HOST="${hostport%%:*}"
  PORT="${hostport##*:}"
  if [[ "$HOST" == "$PORT" ]]; then
    PORT="5432"
  fi
}

parse_host_port "$DATABASE_URL"

echo "[entrypoint] waiting for Postgres at ${HOST}:${PORT} (max ${WAIT_DEADLINE}s)…"
while true; do
  if (echo > "/dev/tcp/${HOST}/${PORT}") 2>/dev/null; then
    break
  fi
  now="$(date +%s)"
  if (( now - START_TS > WAIT_DEADLINE )); then
    echo "[entrypoint] Postgres at ${HOST}:${PORT} never became reachable" >&2
    exit 1
  fi
  sleep 1
done
echo "[entrypoint] Postgres is reachable"

if [[ "${SKIP_ALEMBIC:-0}" != "1" ]]; then
  echo "[entrypoint] running alembic upgrade head"
  uv run alembic upgrade head
else
  echo "[entrypoint] SKIP_ALEMBIC=1 — skipping migrations"
fi

echo "[entrypoint] starting: $*"
exec "$@"
