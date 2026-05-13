"""Chunked Firebase sync — 10k jobs/call, separate HTTP batches, robust to hangs.

Avoids the all-in-one sync_jobs_to_firebase() hanging on a single bad
Firebase batch by splitting into independent offset/limit windows.
Each window fetches its own active rows from Postgres and pushes them.
include_inactive runs only on the final window to avoid redundant work.
"""
import sys
import time
from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

# Total survivors in Postgres = 128,959. Chunk 10k.
CHUNK = 10000
TOTAL = 130000  # round up
offset = 0
while offset < TOTAL:
    is_last = offset + CHUNK >= TOTAL
    t0 = time.time()
    try:
        stats = sync_jobs_to_firebase(
            full_sync=True,
            active_limit=CHUNK,
            active_offset=offset,
            include_inactive=is_last,
        )
        elapsed = time.time() - t0
        print(f"CHUNK offset={offset} active={stats['active_jobs']} "
              f"inactive={stats['inactive_jobs']} synced={stats['synced']} "
              f"batches={stats['batches']} {elapsed:.0f}s", flush=True)
        if stats['active_jobs'] == 0 and not is_last:
            print("CHUNK no active rows — done", flush=True)
            break
    except Exception as e:
        elapsed = time.time() - t0
        print(f"CHUNK offset={offset} FAILED after {elapsed:.0f}s: {e}", flush=True)
        # skip ahead to next chunk so a single bad batch doesn't deadlock progress
    offset += CHUNK

print("ALL_CHUNKS_DONE", flush=True)
