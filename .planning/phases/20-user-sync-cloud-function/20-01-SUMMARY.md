---
phase: 20-user-sync-cloud-function
plan: "01"
subsystem: firebase-core-service
tags: [matching, firebase, supabase, webhook, platform-users]
---

# Phase 20 Plan 01 Summary

Implemented the core-service side of user sync.

## What Changed

- Added a new `matching` service to `wekruit-core-service-cloud-function` with its own runtime config, Firestore collection registry, and root export.
- Implemented `POST /api/sync/user-changed` with `X-Webhook-Signature` validation, Supabase aggregate lookup, `platform-users/{uid}` mapping, and duplicate-delivery deduplication via `sourcePayloadHash`.
- Added local tests that verify the mapping path, 401 behavior for missing signatures, and duplicate webhook suppression.

## Caveats

- Actual Supabase webhook creation in the VALET Supabase project is still an environment step and was not performed from code in this session.

## Verification

- `npm test`
