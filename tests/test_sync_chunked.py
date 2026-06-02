"""Tests for the chunked Firebase sync retry/gap logic (reliability rank 23).

The old script did `offset += CHUNK` on ANY exception, silently skipping a whole
10k window (data never synced) and reporting overall success. These tests pin the
new behaviour: a failed window is retried with backoff, and a window that fails
all retries is recorded as a GAP (run returns it; main() exits non-zero) rather
than silently skipped.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sync_chunked.py"
    spec = importlib.util.spec_from_file_location("sync_chunked", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ok_stats(**over):
    s = {"active_jobs": 10000, "inactive_jobs": 0, "synced": 10000, "batches": 1, "skipped_docs": 0}
    s.update(over)
    return s


def test_all_windows_succeed_no_gaps():
    mod = _load()
    calls = []

    def sync_fn(**kw):
        calls.append(kw["active_offset"])
        # last window returns 0 active -> loop ends early
        return _ok_stats(active_jobs=0) if kw["active_offset"] >= 20000 else _ok_stats()

    gaps = mod.run_chunked_sync(
        chunk=10000, total=30000, sync_fn=sync_fn, sleep_fn=lambda *_a: None
    )
    assert gaps == []
    assert calls[0] == 0 and calls[1] == 10000


def test_transient_failure_is_retried_then_succeeds():
    mod = _load()
    attempts = {"n": 0}

    def sync_fn(**kw):
        if kw["active_offset"] == 0:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RuntimeError("transient 503")
        return _ok_stats(active_jobs=0) if kw["active_offset"] >= 10000 else _ok_stats()

    gaps = mod.run_chunked_sync(
        chunk=10000, total=20000, sync_fn=sync_fn, sleep_fn=lambda *_a: None
    )
    assert gaps == []
    assert attempts["n"] == 3  # failed twice, succeeded on the 3rd


def test_persistent_failure_records_gap_not_silent_skip():
    mod = _load()

    def sync_fn(**kw):
        if kw["active_offset"] == 10000:
            raise RuntimeError("permanent failure")
        return _ok_stats()

    gaps = mod.run_chunked_sync(
        chunk=10000, total=30000, sync_fn=sync_fn, sleep_fn=lambda *_a: None
    )
    # The failed window's offset is recorded — NOT silently skipped.
    assert 10000 in gaps


def test_main_exits_nonzero_on_gaps(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "run_chunked_sync", lambda: [20000])
    assert mod.main() == 1


def test_main_exits_zero_on_full_success(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "run_chunked_sync", lambda: [])
    assert mod.main() == 0
