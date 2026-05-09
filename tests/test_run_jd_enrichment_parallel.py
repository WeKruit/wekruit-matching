"""Parallelism benchmark for Stage 2b orchestrator (P7-M2, 2026-05-09).

Goal: prove run_jd_enrichment(max_workers=N) actually fans out across N threads.

Strategy: monkeypatch the per-route fetcher with a sleeping mock. The mock
holds a barrier of `max_in_flight` so we can both:
  1. Verify wall-time speedup vs sequential
  2. Verify that at least `max_workers` jobs are simultaneously in-flight
     (otherwise sleep + per-job overhead would still let one-worker mode
     look fast — must prove true parallelism, not just "finished quick")

Notes on what we deliberately DON'T mock:
  - get_connection: tests inject a connection_factory yielding a thread-safe
    fake. We do not exercise the real psycopg pool here (would require
    DATABASE_URL).
  - asyncio.run inside _process_one_job: we let it run normally; the mocked
    Greenhouse fetcher is sync (build_ats_job_data return) so asyncio.run
    is a no-op cost.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from wekruit_matching.pipeline.ats_enricher import build_ats_job_data
from wekruit_matching.pipeline.run_jd_enrichment import run_jd_enrichment


# ---------------------------------------------------------------------------
# Thread-safe fake connection — workers each "acquire" their own via factory
# ---------------------------------------------------------------------------

class _ThreadSafeFakeConn:
    """Captures executed UPDATEs from many threads with a lock."""

    def __init__(self):
        self.executed = []
        self.commit_count = 0
        self._lock = threading.Lock()

    def execute(self, query, params=None):
        with self._lock:
            self.executed.append((query, params))

        class _Result:
            def fetchall(self):
                return []

        return _Result()

    def commit(self):
        with self._lock:
            self.commit_count += 1


class _SelectFakeConn:
    """Main-thread connection used only for SELECT pages. Returns batches once."""

    def __init__(self, batches):
        self._batches = list(batches)
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))
        if query.lstrip().startswith("SELECT"):
            rows = self._batches.pop(0) if self._batches else []

            class _Result:
                def __init__(self, rs):
                    self._rs = rs

                def fetchall(self):
                    return self._rs

            return _Result(rows)

        class _Empty:
            def fetchall(self):
                return []

        return _Empty()

    def commit(self):
        pass


def _settings(**overrides):
    defaults = {
        "firecrawl_api_key": "",
        "firecrawl_base_url": "https://api.firecrawl.dev",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_rows(n: int):
    return [
        {
            "job_id": ("a" * 63 + format(i, "x"))[-64:].rjust(64, "0"),
            "company_name": f"Acme{i}",
            "role_title": "Backend Engineer",
            "primary_url": f"https://boards.greenhouse.io/acme{i}/jobs/{i}",
            "ats_apply_url": None,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tracking fetcher — sleeps to simulate latency, records concurrency probe
# ---------------------------------------------------------------------------

class _ConcurrencyProbe:
    def __init__(self):
        self.in_flight = 0
        self.peak_in_flight = 0
        self._lock = threading.Lock()

    def enter(self):
        with self._lock:
            self.in_flight += 1
            if self.in_flight > self.peak_in_flight:
                self.peak_in_flight = self.in_flight

    def exit(self):
        with self._lock:
            self.in_flight -= 1


def _make_sleeping_fetcher(probe: _ConcurrencyProbe, *, sleep_s: float = 0.1):
    def _fetcher(url: str):
        probe.enter()
        try:
            time.sleep(sleep_s)
        finally:
            probe.exit()
        return build_ats_job_data(
            source="greenhouse",
            description_plain=f"JD for {url}",
            qualifications=["Python"],
        )

    return _fetcher


@contextmanager
def _factory_for(write_conn):
    """Connection factory closure: every worker gets the same thread-safe fake.

    The fake's lock makes it safe to share across workers — we only care that
    the fan-out itself happens; the write target can be a single shared mock.
    """
    yield write_conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_parallel_fan_out_beats_sequential_wall_time(monkeypatch):
    """50 jobs at 0.1s each: parallel(10) should be >5x faster than sequential."""
    n_jobs = 50
    sleep_s = 0.1
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        _make_sleeping_fetcher(probe, sleep_s=sleep_s),
    )

    # ---- Sequential baseline ----
    rows = _make_rows(n_jobs)
    seq_select = _SelectFakeConn([rows, []])
    seq_write = _ThreadSafeFakeConn()
    t0 = time.perf_counter()
    seq_stats = run_jd_enrichment(
        conn=seq_select,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
        max_workers=1,
    )
    t_seq = time.perf_counter() - t0

    assert seq_stats["processed"] == n_jobs, (
        f"sequential should process all {n_jobs} jobs, got {seq_stats}"
    )

    # ---- Parallel run ----
    rows2 = _make_rows(n_jobs)
    par_select = _SelectFakeConn([rows2, []])
    par_write = _ThreadSafeFakeConn()

    @contextmanager
    def _factory():
        # Every worker shares the same thread-safe fake — we only care
        # about the fan-out, not write isolation here.
        yield par_write

    probe.peak_in_flight = 0  # reset
    t0 = time.perf_counter()
    par_stats = run_jd_enrichment(
        conn=par_select,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
        max_workers=10,
        connection_factory=_factory,
    )
    t_par = time.perf_counter() - t0

    assert par_stats["processed"] == n_jobs, (
        f"parallel should process all {n_jobs} jobs, got {par_stats}"
    )

    speedup = t_seq / t_par if t_par > 0 else float("inf")
    print(
        f"\n[BENCH] n={n_jobs} sleep={sleep_s}s | "
        f"sequential={t_seq:.3f}s parallel(10)={t_par:.3f}s "
        f"speedup={speedup:.2f}x peak_in_flight={probe.peak_in_flight}"
    )

    # Sequential lower bound: n * sleep_s = 5.0s. Parallel upper bound for
    # max_workers=10: ~ceil(50/10) * sleep_s = 0.5s plus thread scheduling
    # overhead. >5x is comfortably achievable; assert >=5x to leave slack.
    assert speedup >= 5.0, (
        f"Expected >=5x speedup, got {speedup:.2f}x "
        f"(seq={t_seq:.3f}s par={t_par:.3f}s)"
    )

    # Concurrency probe red-line (PUA #3): peak_in_flight must reach close to
    # max_workers — proves fan-out is real, not just "finished quickly".
    assert probe.peak_in_flight >= 8, (
        f"Expected peak_in_flight close to max_workers=10, got {probe.peak_in_flight}"
    )


def test_parallel_per_job_error_isolation(monkeypatch):
    """One job's exception must not stop sibling workers."""
    n_jobs = 20
    failure_indices = {3, 7, 11}
    probe = _ConcurrencyProbe()

    def _flaky_fetcher(url: str):
        # url ends with /jobs/<i>
        idx = int(url.rsplit("/", 1)[-1])
        if idx in failure_indices:
            raise RuntimeError(f"simulated transient failure for {idx}")
        probe.enter()
        try:
            time.sleep(0.02)
        finally:
            probe.exit()
        return build_ats_job_data(
            source="greenhouse",
            description_plain=f"JD for {idx}",
            qualifications=["Python"],
        )

    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        _flaky_fetcher,
    )

    rows = _make_rows(n_jobs)
    select_conn = _SelectFakeConn([rows, []])
    write_conn = _ThreadSafeFakeConn()

    @contextmanager
    def _factory():
        yield write_conn

    stats = run_jd_enrichment(
        conn=select_conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
        max_workers=10,
        connection_factory=_factory,
    )

    assert stats["processed"] == n_jobs, stats
    assert stats["failed"] == len(failure_indices), stats
    # Successful jobs write success row; failed jobs write a failure row.
    # Both pathways hit the write_conn, so total executed == n_jobs.
    assert len(write_conn.executed) == n_jobs, (
        f"expected {n_jobs} writes, got {len(write_conn.executed)}"
    )


def test_parallel_preserves_result_shape(monkeypatch):
    """Result dict keys + types must match sequential mode."""
    monkeypatch.setattr(
        "wekruit_matching.pipeline.run_jd_enrichment.fetch_greenhouse_job",
        lambda url: build_ats_job_data(
            source="greenhouse",
            description_plain="JD",
            qualifications=["Python"],
        ),
    )

    rows = _make_rows(5)
    select_conn = _SelectFakeConn([rows, []])
    write_conn = _ThreadSafeFakeConn()

    @contextmanager
    def _factory():
        yield write_conn

    stats = run_jd_enrichment(
        conn=select_conn,
        settings=_settings(),
        batch_size=500,
        domain_min_interval=0.0,
        max_workers=4,
        connection_factory=_factory,
    )

    expected_keys = {
        "processed",
        "failed",
        "skipped",
        "credits_used",
        "sources",
        "failed_by_source",
        "dry_run",
    }
    assert set(stats.keys()) == expected_keys, stats
    assert isinstance(stats["sources"], dict)
    assert isinstance(stats["failed_by_source"], dict)
    assert stats["dry_run"] is False
    assert stats["processed"] == 5
    assert stats["failed"] == 0


def test_parallel_default_max_workers_is_ten(monkeypatch):
    """Default max_workers per task spec D-T1 must be 10."""
    import inspect

    sig = inspect.signature(run_jd_enrichment)
    assert sig.parameters["max_workers"].default == 10, (
        f"max_workers default must be 10, got {sig.parameters['max_workers'].default}"
    )
    assert sig.parameters["max_workers"].kind == inspect.Parameter.KEYWORD_ONLY, (
        "max_workers must be keyword-only"
    )
