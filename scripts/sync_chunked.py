"""Chunked Firebase sync — 10k jobs/call, separate HTTP batches, robust to hangs.

Avoids the all-in-one sync_jobs_to_firebase() hanging on a single bad
Firebase batch by splitting into independent offset/limit windows.
Each window fetches its own active rows from Postgres and pushes them.
include_inactive runs only on the final window to avoid redundant work.

Reliability (rank 23, 2026-06-01): a failed chunk is RETRIED with bounded
exponential backoff before advancing — the old code did `offset += CHUNK` on
any exception, SILENTLY SKIPPING the whole 10k window (data never synced). On
terminal failure the offset is recorded to a durable list and the script exits
NON-ZERO so the failure is visible instead of looking like success.
"""
import sys
import time

from wekruit_matching.pipeline.job_sync import sync_jobs_to_firebase

# Total survivors in Postgres. Chunk 10k.
CHUNK = 10000
TOTAL = 130000  # round up
MAX_CHUNK_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 5


def run_chunked_sync(
    *,
    chunk: int = CHUNK,
    total: int = TOTAL,
    max_attempts: int = MAX_CHUNK_ATTEMPTS,
    backoff_base: float = BACKOFF_BASE_SECONDS,
    sync_fn=sync_jobs_to_firebase,
    sleep_fn=time.sleep,
) -> list[int]:
    """Page through the corpus in ``chunk``-sized windows, retrying a failed
    window with bounded backoff before advancing. Returns the list of offsets
    that could not be synced after all retries (empty == full success). Never
    silently skips a window.

    ``sync_fn`` / ``sleep_fn`` are injectable for testing.
    """
    offset = 0
    failed_offsets: list[int] = []

    while offset < total:
        is_last = offset + chunk >= total
        stats = None
        last_err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            t0 = time.time()
            try:
                stats = sync_fn(
                    full_sync=True,
                    active_limit=chunk,
                    active_offset=offset,
                    include_inactive=is_last,
                )
                elapsed = time.time() - t0
                print(
                    f"CHUNK offset={offset} active={stats['active_jobs']} "
                    f"inactive={stats['inactive_jobs']} synced={stats['synced']} "
                    f"skipped={stats.get('skipped_docs', 0)} "
                    f"batches={stats['batches']} {elapsed:.0f}s "
                    f"(attempt {attempt})",
                    flush=True,
                )
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                elapsed = time.time() - t0
                print(
                    f"CHUNK offset={offset} attempt {attempt}/{max_attempts} "
                    f"FAILED after {elapsed:.0f}s: {e}",
                    flush=True,
                )
                if attempt < max_attempts:
                    # bounded exponential backoff (5s, 10s, ...) before retry.
                    sleep_fn(backoff_base * (2 ** (attempt - 1)))

        if stats is None:
            # All attempts exhausted — record the gap and KEEP GOING to other
            # windows, but never silently skip: the caller exits non-zero.
            print(
                f"CHUNK offset={offset} GAVE UP after {max_attempts} attempts "
                f"(last error: {last_err}) — recording gap, NOT skipping silently",
                flush=True,
            )
            failed_offsets.append(offset)
        elif stats["active_jobs"] == 0 and not is_last:
            print("CHUNK no active rows — done", flush=True)
            break

        offset += chunk

    return failed_offsets


def main() -> int:
    failed_offsets = run_chunked_sync()
    if failed_offsets:
        print(
            f"ALL_CHUNKS_DONE_WITH_GAPS failed_offsets={failed_offsets} — "
            f"re-run to cover these windows",
            flush=True,
        )
        return 1
    print("ALL_CHUNKS_DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
