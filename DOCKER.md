# wekruit-matching — Docker quick-start (laptop or VPS fallback)

The pipeline was historically pinned to one Mac mini. When that host went dark
on 2026-05-22 the scrape stopped delivering jobs for five days and the active
pool aged out. This Docker setup is the fallback: any machine with Docker can
take over.

## Why you'd use this

- The Mac mini is offline (power, Wi-Fi, Tailscale) and you need fresh jobs in
  the next 24h.
- You're testing pipeline changes locally without touching production data.
- You want to migrate to a VPS or Cloud Run job long term (the same image
  ships everywhere).

## Pre-reqs

- Docker Engine 24+ with Compose v2 (`docker compose` not `docker-compose`).
- API keys for Anthropic, OpenAI, SiliconFlow, GitHub. Optional: Firecrawl,
  Serper.dev, Mailgun.
- A Firebase service-account JSON (the same one the Mac mini uses; pull it
  from 1Password or the macmini `/Users/Shared/wekruit/.firebase-creds`).

## One-time setup

```bash
cd wekruit-matching
cp .env.docker.example .env
chmod 600 .env
# Edit .env: paste keys, paste FIREBASE_SERVICE_ACCOUNT_JSON on one line.

make up
```

That starts:

- `db` — `pgvector/pgvector:pg16`, bound to `127.0.0.1:5433` on the host.
- `app` — the matching pipeline + FastAPI server on `127.0.0.1:8001`.

`make up` runs `alembic upgrade head` automatically inside the entrypoint, so
the schema is current before any pipeline stage runs.

## Daily operations

```bash
make scrape-once       # run one full pipeline cycle (scrape → enrich → embed → sync)
make scrape-daemon     # loop every 24h forever; Ctrl-C to stop
make logs              # tail app logs
make status            # show container health
make db-shell          # psql into Postgres
make app-shell         # bash inside the app container
make down              # stop everything (PG data persists)
make nuke              # stop AND drop the PG volume — DESTROYS local state
```

## Verifying it works

After `make scrape-once` finishes you should see the Stage 4 line:

```
firebaseSync: synced=… failed=… durationMs=…
```

Then on the Firebase side:

```bash
node /Users/adam/Desktop/WeKruit/wekruit-pa/scripts/scrape-health.mjs
```

`newest active syncedAt` should now report `<24h` and `paScrapeFreshnessMonitorDaily`
will go quiet on the next 12:00 UTC tick.

## Production-grade fallback

`docker-compose.yml` sets `restart: unless-stopped` on both services, so
if the host reboots the container comes back up. Combine with:

- `launchd` (macOS): only need to ensure Docker Desktop starts at login.
- `systemd` (Linux): `systemctl enable docker` is enough — compose auto-restarts.

For a true 24/7 fallback, run this stack on a small Linux VPS ($5–10/month)
or push the image to Cloud Run jobs (Phase B follow-up).

## Limitations / follow-ups

- **PG holds dedupe state today.** If the laptop Docker volume is lost,
  the next scrape will treat every job as new (cost spike). The Phase B
  effort migrates `dedup_skipped_signatures` + `dead_url_tombstone` to
  Firestore so dedupe survives container loss.
- **No Firecrawl container.** This stack treats Firecrawl as a hosted HTTP
  service via `FIRECRAWL_API_KEY`. If you need a local Firecrawl, add a
  third service to `docker-compose.yml` pointing at the public `firecrawl`
  image and update `FIRECRAWL_BASE_URL`.
- **No cron.** Use `make scrape-daemon` or set up a system cron that runs
  `make scrape-once`. Production hosts should rely on Cloud Scheduler →
  Cloud Run job once that migration lands.

## Recovery playbook reference

If you got here because of an active scrape outage, the parent runbook lives
at `wekruit-pa/.planning/RUNBOOK-scrape-dead.md`.
