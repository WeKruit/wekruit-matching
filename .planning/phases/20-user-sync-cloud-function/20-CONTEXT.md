# Phase 20 Context

- Phase 20 had to land in `wekruit-core-service-cloud-function`, not inside `wekruit-matching`, because `platform-users` is owned by Firebase Core Service.
- The implementation constraint stayed fixed: zero VALET code changes, Supabase Database Webhooks as the trigger, and Firestore as the read model.
- The core-service implementation now includes:
  - `POST /api/sync/user-changed`
  - `GET /health`
  - a `matching` service registry entry in `src/index.ts`
  - `platform-users` mapping with deduplication via `sourcePayloadHash`
- The webhook auth model uses a shared `X-Webhook-Signature` secret header. This matches Supabase DB Webhooks' custom-header capability and avoids introducing a VALET-side change.
