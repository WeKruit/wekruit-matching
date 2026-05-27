-- Idempotent Postgres init for wekruit-matching.
--
-- pgvector/pgvector:pg16 already ships the `vector` extension binary; we just
-- need to enable it inside the application database. Alembic migrations
-- assume the extension is present, so this runs once at first-boot via the
-- official Postgres `docker-entrypoint-initdb.d` hook.
CREATE EXTENSION IF NOT EXISTS vector;
