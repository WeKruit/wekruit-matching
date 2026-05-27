#!/bin/bash
# 2026-05-27: removed hardcoded `/Users/wekruitclaw1/...` path so this script
# runs from any clone (Adam's laptop fallback, CI runners, fresh VPS).
# Resolves the repo root relative to this file.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec .venv/bin/uvicorn wekruit_matching.api.server:app --host 127.0.0.1 --port 8001 --workers 4
