# Phase 19 Context

- BOARD-04 is the documentation gate for the entire v2.0 milestone: Phase 20 and Phase 21 should not start until the platform handoff reflects the actual Mac Mini runtime and Firebase boundaries.
- `WEKRUIT-PLATFORM-HANDOFF.md` already covered target schemas and API flows, but it was still missing the operational layer: launchd plists, log paths, Mac Mini setup steps, Firecrawl's 5-container Docker topology, and the Firestore collection prefix rules.
- Runtime facts verified from code and checked-in docs:
  - `start-server.sh` binds uvicorn to `127.0.0.1:8001`
  - `scripts/daily-update.sh` runs `python -m wekruit_matching.pipeline.daily`
  - the internal pipeline UI points operators to `/tmp/matching-daily-update.log`
  - `firecrawl-selfhost/docker-compose.yaml` defines `api`, `playwright-service`, `redis`, `rabbitmq`, and `nuq-postgres`
  - `firecrawl-selfhost/docker-compose.override.yaml` lowers memory/CPU for the 16GB Mac Mini
