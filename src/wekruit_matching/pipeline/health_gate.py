"""Post-run reliability / data-quality gate for the daily pipeline.

Why this exists
===============
``run_daily_pipeline`` computes ``pipeline_status`` purely from whether each
stage *raised* (crash/timeout). A stage can finish cleanly while still leaving
the matchable corpus collapsed: ``embed_all`` can under-produce or no-op, the
JD-extraction path can stall, and the run is still reported ``success``. The
live matcher (a separate Cloud Function reading the Firestore ``matching-jobs``
collection) then serves a corpus that silently shrank -- and the regression is
discovered by users, not by the pipeline ("a new issue every day").

This module closes that gap. As a final stage of the daily run it:

  1. ``compute_metrics(conn)`` -- reads coverage + reconciliation metrics from
     the live DB, including the EXACT Firestore sync-gate predicate (the real
     "matchable corpus") used by ``job_sync._fetch_active_jobs``.
  2. ``evaluate(metrics, prior, thresholds)`` -- a pure function returning a
     list of structured failures, comparing against absolute floors AND the
     previous run's metrics (run-over-run drop detection).
  3. ``save_state`` / ``load_prior_state`` -- atomically persist this run's
     metrics so the next run can detect drops.

``run_health_gate()`` ties these together and returns
``{"ok": bool, "metrics": dict, "failures": [...]}``. The orchestrator wires
it in as the last stage: any failure flips the run to non-zero exit and the
failures are listed in the completion email.

Threshold philosophy (calibrated to the live baseline 2026-05-29)
----------------------------------------------------------------
Absolute floors apply ONLY to robust, always-high signals so normal jitter
never trips them:

  * embedded/sync-gate coverage of active -> floor 0.97  (baseline 0.7813
    today is BELOW this on purpose: ~6k active jobs are not matchable, the live
    defect this gate is meant to make loud)
  * embeddable-but-unembedded backlog      -> max 300    (baseline 34)
  * active job count                        -> min 1      (never empty)

Fields whose coverage is legitimately low and source-dependent (sponsorship
0.169, seniority 0.206 today) get NO absolute floor -- they are guarded ONLY
by relative drop detection vs the prior run, so a healthy-but-low first run
passes and only a real regression trips them.

Run standalone:  python -m wekruit_matching.pipeline.health_gate
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from loguru import logger

from wekruit_matching.db.connection import get_connection


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------
DEFAULT_THRESHOLDS: dict[str, float] = {
    # Absolute floors (apply even on the first run / no prior state).
    "min_embedded_cov_of_active": 0.97,
    "max_embeddable_unembedded_backlog": 300,
    "min_active": 1,
    # Relative drop guards (apply only when a prior run exists).
    "max_matchable_drop_frac": 0.10,   # matchable corpus not down >10% vs prior
    "max_active_drop_frac": 0.10,      # active count not down >10% vs prior
    # Per-field coverage relative guard: coverage must not fall by more than
    # this many ABSOLUTE points vs the prior run. Catches gradual rot on the
    # source-dependent fields without false-positiving on their low baseline.
    "max_coverage_drop_points": 0.05,
}

# Where the previous run's metrics live. Kept OUT of the repo and DB on
# purpose: it is run-local operational state, not source-of-truth data.
# Overridable via env for the cron/launchd environment.
DEFAULT_STATE_PATH = Path(
    os.environ.get("HEALTH_GATE_STATE", "/tmp/wekruit_health_gate_state.json")
)

# Fields tracked for relative coverage-drop detection (key -> human label).
_COVERAGE_FIELDS: dict[str, str] = {
    "embedded_cov_of_active": "embedded",
    "industry_cov_of_enriched": "industry",
    "skills_nonempty_cov_of_enriched": "non-empty skills",
    "sponsorship_cov_of_enriched": "sponsorship",
    "seniority_cov_of_enriched": "seniority",
}


# ---------------------------------------------------------------------------
# Metric computation (live DB)
# ---------------------------------------------------------------------------
def _scalar(conn, sql: str) -> int:
    return int(conn.execute(sql).fetchone()["count"])


def compute_metrics(conn) -> dict[str, Any]:
    """Compute coverage + reconciliation metrics from the live DB.

    ``conn`` is a live psycopg connection (dict_row factory, as produced by
    ``db.connection.get_connection``). Returns a flat dict of ints/floats that
    ``evaluate`` consumes. All ratios are guarded against divide-by-zero.

    The "matchable corpus" is computed with the EXACT predicate used by
    ``job_sync._fetch_active_jobs`` so the gate measures what the live matcher
    actually receives, not a looser proxy.
    """
    active = _scalar(conn, "SELECT count(*) FROM jobs WHERE status='active'")
    active_enriched = _scalar(
        conn,
        "SELECT count(*) FROM jobs WHERE status='active' "
        "AND enriched_at IS NOT NULL",
    )
    active_embedded = _scalar(
        conn,
        "SELECT count(*) FROM jobs WHERE status='active' "
        "AND embedding IS NOT NULL",
    )
    # Matchable corpus == exact Firestore sync gate (job_sync._fetch_active_jobs).
    matchable = _scalar(
        conn,
        """
        SELECT count(*) FROM jobs
        WHERE status='active'
          AND embedding IS NOT NULL
          AND embedded_at IS NOT NULL
          AND job_description IS NOT NULL
          AND length(job_description) >= 200
          AND required_skills IS NOT NULL
          AND cardinality(required_skills) > 0
        """,
    )
    # Backlog of jobs that ARE embeddable (have JD body + skills) but were not
    # embedded -- the embed stage is the bottleneck for these.
    embeddable_unembedded = _scalar(
        conn,
        """
        SELECT count(*) FROM jobs
        WHERE status='active'
          AND embedding IS NULL
          AND job_description IS NOT NULL
          AND length(job_description) >= 200
          AND required_skills IS NOT NULL
          AND cardinality(required_skills) > 0
        """,
    )
    spons = _scalar(
        conn,
        "SELECT count(*) FROM jobs WHERE status='active' "
        "AND enriched_at IS NOT NULL AND sponsorship IS NOT NULL",
    )
    sen = _scalar(
        conn,
        "SELECT count(*) FROM jobs WHERE status='active' "
        "AND enriched_at IS NOT NULL AND seniority_level IS NOT NULL",
    )
    ind = _scalar(
        conn,
        "SELECT count(*) FROM jobs WHERE status='active' "
        "AND enriched_at IS NOT NULL AND industry IS NOT NULL",
    )
    skills = _scalar(
        conn,
        "SELECT count(*) FROM jobs WHERE status='active' "
        "AND enriched_at IS NOT NULL AND required_skills IS NOT NULL "
        "AND cardinality(required_skills) > 0",
    )

    def _ratio(n: int, d: int) -> float:
        return (n / d) if d else 0.0

    return {
        "active": active,
        "active_enriched": active_enriched,
        "active_embedded": active_embedded,
        "matchable_corpus": matchable,
        "embeddable_unembedded_backlog": embeddable_unembedded,
        "embedded_cov_of_active": _ratio(active_embedded, active),
        "sponsorship_cov_of_enriched": _ratio(spons, active_enriched),
        "seniority_cov_of_enriched": _ratio(sen, active_enriched),
        "industry_cov_of_enriched": _ratio(ind, active_enriched),
        "skills_nonempty_cov_of_enriched": _ratio(skills, active_enriched),
    }


# ---------------------------------------------------------------------------
# Evaluation (pure -- testable with synthetic dicts)
# ---------------------------------------------------------------------------
def _fail(metric: str, value: Any, threshold: Any, message: str) -> dict:
    return {
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "message": message,
    }


def evaluate(
    metrics: dict[str, Any],
    prior: Optional[dict[str, Any]] = None,
    thresholds: Optional[dict[str, float]] = None,
) -> list[dict]:
    """Return a list of failure dicts (empty == healthy).

    Pure function: no DB, no I/O. ``metrics`` is the current run; ``prior`` is
    the previous run's metrics (or None on the first run, which disables the
    relative drop checks).
    """
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    failures: list[dict] = []

    def g(d: Optional[dict], k: str, default=None):
        return d.get(k, default) if d else default

    # --- absolute floors ---------------------------------------------------
    active = g(metrics, "active", 0) or 0
    if active < t["min_active"]:
        failures.append(
            _fail("active", active, t["min_active"],
                  f"active job count {active} below floor {t['min_active']} "
                  "(corpus collapsed)")
        )

    emb_cov = g(metrics, "embedded_cov_of_active")
    if emb_cov is not None and emb_cov < t["min_embedded_cov_of_active"]:
        not_matchable = None
        if g(metrics, "active") and g(metrics, "matchable_corpus") is not None:
            not_matchable = metrics["active"] - metrics["matchable_corpus"]
        suffix = (
            f" ({not_matchable} active jobs not matchable)"
            if not_matchable is not None else ""
        )
        failures.append(
            _fail("embedded_cov_of_active", round(emb_cov, 4),
                  t["min_embedded_cov_of_active"],
                  f"embedded coverage of active {emb_cov:.4f} below "
                  f"{t['min_embedded_cov_of_active']:.2f}{suffix}")
        )

    backlog = g(metrics, "embeddable_unembedded_backlog")
    if backlog is not None and backlog > t["max_embeddable_unembedded_backlog"]:
        failures.append(
            _fail("embeddable_unembedded_backlog", backlog,
                  t["max_embeddable_unembedded_backlog"],
                  f"embeddable-but-unembedded backlog {backlog} exceeds "
                  f"{t['max_embeddable_unembedded_backlog']} (these jobs have "
                  "a JD + skills but no embedding -> not matchable)")
        )

    # --- relative guards (need a prior run) --------------------------------
    if prior:
        for key, frac_key, label in (
            ("matchable_corpus", "max_matchable_drop_frac", "matchable corpus"),
            ("active", "max_active_drop_frac", "active count"),
        ):
            cur = g(metrics, key)
            prev = g(prior, key)
            if cur is not None and prev:
                drop = (prev - cur) / prev
                if drop > t[frac_key]:
                    failures.append(
                        _fail(key, cur, prev,
                              f"{label} dropped {drop * 100:.1f}% vs prior run "
                              f"({prev} -> {cur}); max allowed "
                              f"{t[frac_key] * 100:.0f}%")
                    )

        for key, label in _COVERAGE_FIELDS.items():
            cur = g(metrics, key)
            prev = g(prior, key)
            if cur is not None and prev is not None:
                drop_pts = prev - cur
                if drop_pts > t["max_coverage_drop_points"]:
                    failures.append(
                        _fail(key, round(cur, 4), round(prev, 4),
                              f"{label} coverage regressed {drop_pts * 100:.1f} "
                              f"pts vs prior ({prev:.4f} -> {cur:.4f})")
                    )

    return failures


def summarize_failures(failures: list[dict]) -> str:
    """Human-readable multi-line summary for logs / email."""
    if not failures:
        return "health gate: OK (no failures)"
    lines = [f"health gate: {len(failures)} FAILURE(S)"]
    for f in failures:
        lines.append(f"  - [{f['metric']}] {f['message']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prior-run state persistence (atomic write, fail-soft read)
# ---------------------------------------------------------------------------
def load_prior_state(path: Path = DEFAULT_STATE_PATH) -> Optional[dict]:
    """Load the previous run's metrics. Returns None if missing or corrupt.

    A corrupt/absent prior must NEVER crash the gate -- it just means the
    relative drop checks are skipped for this run.
    """
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError, ValueError) as e:  # noqa: BLE001
        logger.warning(f"[health_gate] ignoring corrupt prior state {path}: {e}")
        return None
    if isinstance(data, dict):
        return data.get("metrics", data)
    return None


def save_state(metrics: dict, path: Path = DEFAULT_STATE_PATH) -> None:
    """Atomically persist current metrics for the next run's drop checks."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"metrics": metrics}, default=str, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
        os.replace(tmp, path)  # atomic on POSIX
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def run_health_gate(
    state_path: Path = DEFAULT_STATE_PATH,
    thresholds: Optional[dict[str, float]] = None,
) -> dict:
    """Compute metrics from the live DB, evaluate vs thresholds + prior run,
    persist the new metrics, and return a result dict.

    Returns ``{"ok": bool, "metrics": dict, "failures": [...]}``.
    """
    prior = load_prior_state(state_path)
    with get_connection() as conn:
        metrics = compute_metrics(conn)
    failures = evaluate(metrics, prior=prior, thresholds=thresholds)

    # Persist current metrics even when the gate fails, so a one-off cliff
    # doesn't permanently wedge the relative checks against a stale-high
    # baseline.
    try:
        save_state(metrics, state_path)
    except OSError as e:  # noqa: BLE001
        logger.warning(f"[health_gate] could not persist state: {e}")

    ok = not failures
    if ok:
        logger.success(
            "[health_gate] PASS "
            f"active={metrics['active']} matchable={metrics['matchable_corpus']} "
            f"emb_cov={metrics['embedded_cov_of_active']:.4f} "
            f"backlog={metrics['embeddable_unembedded_backlog']}"
        )
    else:
        logger.error(summarize_failures(failures))

    return {"ok": ok, "metrics": metrics, "failures": failures}


def main() -> int:
    result = run_health_gate()
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    import sys

    logger.remove()
    logger.add(sys.stderr, level="INFO")
    sys.exit(main())
