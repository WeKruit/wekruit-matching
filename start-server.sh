#!/bin/bash
cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching
exec .venv/bin/uvicorn wekruit_matching.api.server:app --host 127.0.0.1 --port 8001 --workers 4
