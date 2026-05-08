"""Tests for the parallelized `enrich_pending` worker.

These tests run with NO database — `get_connection` is monkey-patched to
yield a thread-local fake connection, and `classify_job` is replaced with a
sleeper that simulates Qwen3-8B HTTP latency.

Acceptance criteria (P7-A, 2026-05-08):
  - Wall-time for N jobs with 10 workers is < (N * latency / 5)  -> proves
    real parallelism, not sequential pretending to be parallel
  - Per-job exceptions do NOT halt the batch — sibling jobs still complete
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class _FakeReadConn:
    """Connection used only for the main-thread SELECT in enrich_pending."""

    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append((query, params))

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def fetchall(self):
                return self._rows

        return _Result(self._rows)

    def commit(self):
        pass

    def rollback(self):
        pass


class _FakeWorkerConn:
    """Per-worker fake connection. Captures UPDATEs in a thread-safe list."""

    _all_writes_lock = threading.Lock()
    all_writes: list[dict] = []

    def execute(self, query, params=None):
        # The worker only issues UPDATE — record params for verification.
        if "UPDATE" in query.upper():
            with self._all_writes_lock:
                self.all_writes.append(dict(params))

        class _Result:
            def fetchall(self):
                return []

        return _Result()

    def commit(self):
        pass

    def rollback(self):
        pass

    @classmethod
    def reset_writes(cls):
        with cls._all_writes_lock:
            cls.all_writes.clear()


@contextmanager
def _fake_get_connection():
    """Context manager replacement for wekruit_matching.db.connection.get_connection."""
    yield _FakeWorkerConn()


def _make_rows(n: int) -> list[dict]:
    return [
        {
            "job_id": f"{i:064x}",
            "source_repo": "Summer2026-Internships",
            "company_name": f"TestCo{i}",
            "role_title": "SWE Intern",
            "location_raw": "SF",
            "content_hash": f"{i:064x}",
            "job_description": None,
            "required_skills": [],
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_parallel_completes_faster_than_sequential():
    """20 jobs * 0.3s latency in 10 workers must finish in < (20*0.3)/5 = 1.2s.

    The 5x speedup ratio is a conservative lower bound on a 10-worker pool;
    real speedup is closer to 10x when classify_job blocks on I/O.
    """
    from wekruit_matching.enrichment import worker as worker_module
    from wekruit_matching.enrichment.classifier import EnrichmentResult

    n_jobs = 20
    per_job_latency = 0.3  # seconds
    sequential_total = n_jobs * per_job_latency  # 6.0s
    parallel_threshold = sequential_total / 5  # 1.2s — proves >5x parallelism

    _FakeWorkerConn.reset_writes()
    rows = _make_rows(n_jobs)
    read_conn = _FakeReadConn(rows)

    def slow_classify(job):
        time.sleep(per_job_latency)
        return EnrichmentResult(
            industry="tech",
            company_size="startup",
            required_skills=["python"],
            sponsorship=True,
        )

    # Patch get_connection in the worker module's namespace.
    with patch.object(worker_module, "classify_job", side_effect=slow_classify), \
         patch.object(worker_module, "get_connection", _fake_get_connection):
        start = time.perf_counter()
        result = worker_module.enrich_pending(read_conn, max_workers=10)
        elapsed = time.perf_counter() - start

    # Print so pytest -s shows the timing evidence (red-line two: wall-time data)
    print(
        f"\n[parallel-bench] n={n_jobs} latency={per_job_latency}s "
        f"sequential_would_be={sequential_total:.2f}s "
        f"parallel_actual={elapsed:.2f}s "
        f"speedup={sequential_total / elapsed:.2f}x"
    )

    assert result == {"enriched": n_jobs, "failed": 0, "skipped": 0}
    assert elapsed < parallel_threshold, (
        f"Parallel run took {elapsed:.2f}s but must be under {parallel_threshold:.2f}s "
        f"(=sequential/5). Workers may not be running concurrently."
    )
    # Every job got a write
    assert len(_FakeWorkerConn.all_writes) == n_jobs


def test_sequential_baseline_for_comparison():
    """Same workload with max_workers=1 to give a real sequential reference.

    This makes the speedup claim falsifiable — if both runs take the same
    time, parallelism is broken regardless of the absolute threshold.
    """
    from wekruit_matching.enrichment import worker as worker_module
    from wekruit_matching.enrichment.classifier import EnrichmentResult

    n_jobs = 20
    per_job_latency = 0.3

    _FakeWorkerConn.reset_writes()
    rows = _make_rows(n_jobs)
    read_conn = _FakeReadConn(rows)

    def slow_classify(job):
        time.sleep(per_job_latency)
        return EnrichmentResult(
            industry="tech",
            company_size="startup",
            required_skills=["python"],
            sponsorship=True,
        )

    with patch.object(worker_module, "classify_job", side_effect=slow_classify), \
         patch.object(worker_module, "get_connection", _fake_get_connection):
        start = time.perf_counter()
        result = worker_module.enrich_pending(read_conn, max_workers=1)
        elapsed = time.perf_counter() - start

    print(
        f"\n[sequential-bench] n={n_jobs} latency={per_job_latency}s "
        f"actual={elapsed:.2f}s (max_workers=1)"
    )

    assert result == {"enriched": n_jobs, "failed": 0, "skipped": 0}
    # Must be at least close to the sequential bound — proves the
    # comparison test isn't "fast because of test infra"
    assert elapsed >= n_jobs * per_job_latency * 0.8, (
        f"Sequential run finished in {elapsed:.2f}s — suspiciously fast for "
        f"a real serial run of {n_jobs} * {per_job_latency}s tasks."
    )


def test_per_job_failure_does_not_halt_batch():
    """One classify_job raising must not kill sibling jobs."""
    from wekruit_matching.enrichment import worker as worker_module
    from wekruit_matching.enrichment.classifier import EnrichmentResult

    n_jobs = 20
    fail_indices = {3, 7, 11}  # three of twenty raise
    _FakeWorkerConn.reset_writes()
    rows = _make_rows(n_jobs)
    read_conn = _FakeReadConn(rows)

    good = EnrichmentResult(
        industry="tech",
        company_size="startup",
        required_skills=["python"],
        sponsorship=True,
    )

    def flaky_classify(job):
        # Look up this job's position in the input rows by job_id (index encoded)
        idx = int(job.job_id, 16)
        time.sleep(0.05)  # tiny I/O simulation
        if idx in fail_indices:
            raise RuntimeError(f"simulated API failure for job {idx}")
        return good

    with patch.object(worker_module, "classify_job", side_effect=flaky_classify), \
         patch.object(worker_module, "get_connection", _fake_get_connection):
        result = worker_module.enrich_pending(read_conn, max_workers=10)

    expected_enriched = n_jobs - len(fail_indices)
    assert result == {
        "enriched": expected_enriched,
        "failed": len(fail_indices),
        "skipped": 0,
    }
    # Only the successful jobs wrote rows
    assert len(_FakeWorkerConn.all_writes) == expected_enriched


def test_signature_accepts_max_workers_keyword():
    """The acceptance criterion: enrich_pending(conn, *, max_workers=10) is callable."""
    import inspect

    from wekruit_matching.enrichment.worker import enrich_pending

    sig = inspect.signature(enrich_pending)
    params = sig.parameters
    assert "max_workers" in params, (
        "enrich_pending must accept a max_workers kwarg per P7-A acceptance criteria"
    )
    assert params["max_workers"].default == 10, (
        f"Default max_workers must be 10; got {params['max_workers'].default}"
    )
    assert params["max_workers"].kind == inspect.Parameter.KEYWORD_ONLY, (
        "max_workers must be keyword-only (after *) so positional callers stay compatible"
    )


def test_returns_correct_shape_when_no_jobs():
    """No-rows path still returns the {enriched, failed, skipped} shape."""
    from wekruit_matching.enrichment.worker import enrich_pending

    read_conn = _FakeReadConn(rows=[])
    result = enrich_pending(read_conn, max_workers=10)
    assert result == {"enriched": 0, "failed": 0, "skipped": 0}


@pytest.mark.parametrize("workers", [1, 5, 10])
def test_workers_are_actually_concurrent(workers):
    """Track concurrent callers; with 10 workers we must see >1 in flight at once."""
    from wekruit_matching.enrichment import worker as worker_module
    from wekruit_matching.enrichment.classifier import EnrichmentResult

    n_jobs = 30
    in_flight = {"now": 0, "max": 0}
    lock = threading.Lock()

    def tracking_classify(job):
        with lock:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
        time.sleep(0.1)
        with lock:
            in_flight["now"] -= 1
        return EnrichmentResult(
            industry="tech",
            company_size="startup",
            required_skills=[],
            sponsorship=None,
        )

    _FakeWorkerConn.reset_writes()
    rows = _make_rows(n_jobs)
    read_conn = _FakeReadConn(rows)

    with patch.object(worker_module, "classify_job", side_effect=tracking_classify), \
         patch.object(worker_module, "get_connection", _fake_get_connection):
        worker_module.enrich_pending(read_conn, max_workers=workers)

    print(f"\n[concurrency-probe] workers={workers} max_in_flight={in_flight['max']}")
    if workers == 1:
        assert in_flight["max"] == 1
    else:
        assert in_flight["max"] >= 2, (
            f"With max_workers={workers} we observed peak in-flight={in_flight['max']}; "
            "expected >=2. Workers are not running in parallel."
        )
