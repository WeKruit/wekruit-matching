"""Firestore-backed distributed lock for the daily scrape pipeline.

Why this exists
---------------
The pipeline historically ran on a single Mac mini launchd job. When the host
went dark on 2026-05-22 the daily scrape stopped for five days. The fix
(`docker-compose` for any-host portability + GitHub Actions cron) introduces
a new failure mode: if the Mac mini comes back online while GH Actions has
already run today, both jobs will write to ``matching-jobs`` and double-spend
on LLM enrichment.

Distributed lock semantics:
  * Key:        ``scrape-daily-YYYY-MM-DD`` in UTC.
  * Collection: ``pa-system-locks``.
  * Acquire:    Firestore ``doc.create({...})`` — atomic; fails if the doc
                already exists. The losing process exits 0 (not a failure;
                another runner is already on it) with a clear log line.
  * Stale:      A lock older than ``STALE_AFTER_SECONDS`` (default 4h) is
                considered abandoned — the next acquirer "steals" it by
                deleting + re-creating. Prevents a crashed runner from
                blocking the day.
  * Release:    Sets ``releasedAt`` (keeps the doc for audit). A subsequent
                same-day acquire sees a released lock and refuses with a
                distinct exit code so we don't re-run by accident.

Audit trail:
  Every successful run leaves a permanent ``pa-system-locks`` doc with
  ``acquiredAt`` / ``acquiredBy`` / ``releasedAt`` / ``outcome``. Useful
  for Claude routine + on-call to answer "did today's scrape run? where?".

Usage (Python):
    from wekruit_matching.lock import DailyScrapeLock, LockState

    with DailyScrapeLock(acquired_by="github-actions:ubuntu-22.04") as lock:
        if lock.state is LockState.CONTENDED:
            sys.exit(0)  # another runner has the lock; quiet exit
        if lock.state is LockState.ALREADY_RUN:
            sys.exit(0)  # today's run already completed
        # ... run pipeline ...
        lock.mark_outcome("success", stats=stats_dict)

Usage (CLI wrapper):
    python -m wekruit_matching.lock acquire --acquired-by "$HOSTNAME"
    # exit 0 = acquired or already-run; exit 1 = contended (don't proceed)
    python -m wekruit_matching.lock release --outcome success
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from google.api_core.exceptions import AlreadyExists
from google.cloud import firestore

# ---------------------------------------------------------------------------
# Tunables — single place so an on-call agent can change without grepping.
# ---------------------------------------------------------------------------

#: Top-level Firestore collection holding the lock docs.
LOCK_COLLECTION = "pa-system-locks"

#: Lock-key prefix; the daily date is appended.
LOCK_KEY_PREFIX = "scrape-daily-"

#: A lock older than this without a ``releasedAt`` is treated as abandoned
#: and may be stolen by the next acquirer.
STALE_AFTER_SECONDS = 4 * 3600  # 4h

#: Where the per-process state file lives so ``acquire`` + ``release``
#: invocations from a wrapper shell script can hand the lock ID across.
DEFAULT_STATE_FILE = Path("/tmp/wekruit-scrape-lock.json")


class LockState(str, enum.Enum):
    """High-level acquisition outcome.

    The string values are also what the CLI prints to stdout so a shell
    wrapper can branch on them without parsing JSON.
    """

    ACQUIRED = "acquired"  # this runner now owns the day's lock
    CONTENDED = "contended"  # another runner currently holds the lock
    ALREADY_RUN = "already_run"  # today already completed successfully
    STOLEN_STALE = "stolen_stale"  # previous holder timed out; we took over


@dataclass
class LockResult:
    """Returned by ``DailyScrapeLock.acquire`` so callers can branch."""

    state: LockState
    lock_id: str
    """Firestore document ID — needed by ``release``."""
    acquired_at_ms: int | None = None
    """Server-side acquired timestamp (ms since epoch). None on contention."""
    holder: str | None = None
    """Who currently holds the lock (informational; set on contention)."""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "lock_id": self.lock_id,
            "acquired_at_ms": self.acquired_at_ms,
            "holder": self.holder,
            "note": self.note,
        }


@dataclass
class DailyScrapeLock:
    """Context-manager friendly wrapper around the daily lock doc.

    Construction takes only metadata. ``acquire`` is the network call;
    ``release`` finalizes. Use as either a context manager or imperatively.
    """

    acquired_by: str = field(default_factory=socket.gethostname)
    """Free-form identifier for the runner. Stored verbatim in Firestore."""

    now_fn: Any = field(default=lambda: datetime.now(timezone.utc))
    """Injection seam for tests. Production passes ``datetime.now(UTC)``."""

    state_file: Path = DEFAULT_STATE_FILE
    """Where ``acquire`` writes the lock ID so a sibling ``release``
    invocation can find it. Tests override with a tmp path."""

    client: firestore.Client | None = None
    """Optional injected Firestore client. Production lets us build one
    from ``FIREBASE_SERVICE_ACCOUNT_JSON`` or default ADC."""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def acquire(self) -> LockResult:
        """Try to take today's lock. Idempotent.

        Returns one of four states; never raises for the routine failures
        (contended / already-run / stolen) — only raises if Firestore
        itself is unreachable, which is genuinely a hard error.
        """
        client = self._get_client()
        lock_id = self._today_lock_id()
        doc_ref = client.collection(LOCK_COLLECTION).document(lock_id)
        now = self.now_fn()
        now_ms = int(now.timestamp() * 1000)

        payload = {
            "lockKey": lock_id,
            "acquiredAt": now,
            "acquiredAtMs": now_ms,
            "acquiredBy": self.acquired_by,
            "expiresAtMs": now_ms + STALE_AFTER_SECONDS * 1000,
            "releasedAt": None,
            "outcome": None,
            "version": 1,
        }

        # First, try to create the doc atomically. If it doesn't exist this
        # wins immediately. ``AlreadyExists`` means someone else already
        # has it OR the day already finished — we branch on what's inside.
        try:
            doc_ref.create(payload)
            self._persist_state({"lock_id": lock_id, "acquired_by": self.acquired_by})
            return LockResult(
                state=LockState.ACQUIRED,
                lock_id=lock_id,
                acquired_at_ms=now_ms,
                holder=self.acquired_by,
            )
        except AlreadyExists:
            pass

        # Doc exists — read it to decide.
        snap = doc_ref.get()
        if not snap.exists:
            # Race: it existed, then got deleted between create + get. Retry
            # once and treat the result as authoritative.
            try:
                doc_ref.create(payload)
                self._persist_state({"lock_id": lock_id, "acquired_by": self.acquired_by})
                return LockResult(
                    state=LockState.ACQUIRED,
                    lock_id=lock_id,
                    acquired_at_ms=now_ms,
                    holder=self.acquired_by,
                )
            except AlreadyExists:
                snap = doc_ref.get()

        existing = snap.to_dict() or {}
        holder = existing.get("acquiredBy", "unknown")

        # If the existing lock has been released successfully we treat the
        # day as done. A separate runner shouldn't retry the same date.
        if existing.get("releasedAt") is not None and existing.get("outcome") == "success":
            return LockResult(
                state=LockState.ALREADY_RUN,
                lock_id=lock_id,
                holder=holder,
                note=f"already completed at {existing.get('releasedAt')}",
            )

        # If it's stale (older than the threshold and not released), steal it.
        # Stealing is best-effort: if another runner steals at the same moment
        # only one of the create() calls below will win.
        expires_ms = existing.get("expiresAtMs") or 0
        if existing.get("releasedAt") is None and expires_ms < now_ms:
            doc_ref.delete()
            try:
                doc_ref.create({**payload, "stolenFrom": holder})
                self._persist_state({"lock_id": lock_id, "acquired_by": self.acquired_by})
                return LockResult(
                    state=LockState.STOLEN_STALE,
                    lock_id=lock_id,
                    acquired_at_ms=now_ms,
                    holder=self.acquired_by,
                    note=f"stale lock previously held by {holder}",
                )
            except AlreadyExists:
                snap = doc_ref.get()
                holder = (snap.to_dict() or {}).get("acquiredBy", "unknown")

        # Someone else currently holds an active lock. Quiet exit.
        return LockResult(
            state=LockState.CONTENDED,
            lock_id=lock_id,
            holder=holder,
            note="another runner currently holds the lock",
        )

    def release(self, outcome: str, stats: dict[str, Any] | None = None) -> None:
        """Mark today's lock as completed.

        ``outcome`` is a short tag — ``"success"``, ``"partial"``,
        ``"failed"``. ``stats`` is whatever the caller wants to keep for
        audit. Tolerant of double-release.
        """
        client = self._get_client()
        lock_id = self._load_state().get("lock_id") or self._today_lock_id()
        doc_ref = client.collection(LOCK_COLLECTION).document(lock_id)
        now = self.now_fn()
        update = {
            "releasedAt": now,
            "releasedAtMs": int(now.timestamp() * 1000),
            "outcome": outcome,
        }
        if stats is not None:
            update["stats"] = stats
        doc_ref.set(update, merge=True)

    # ------------------------------------------------------------------
    # Context-manager sugar — `with DailyScrapeLock(...) as lock:`
    # ------------------------------------------------------------------

    def __enter__(self) -> "DailyScrapeLock":
        self.last_result = self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # On exception, mark "failed". On clean exit without explicit
        # mark_outcome, mark "success" only if we held the lock.
        outcome_attr = getattr(self, "_outcome", None)
        stats_attr = getattr(self, "_stats", None)
        if self.last_result.state in (LockState.ACQUIRED, LockState.STOLEN_STALE):
            outcome = outcome_attr or ("failed" if exc_type else "success")
            self.release(outcome=outcome, stats=stats_attr)
        return False  # never suppress exceptions

    # ------------------------------------------------------------------
    # Test ergonomics
    # ------------------------------------------------------------------

    def mark_outcome(self, outcome: str, stats: dict[str, Any] | None = None) -> None:
        """Set the outcome string the context manager will use on exit."""
        self._outcome = outcome
        self._stats = stats

    @property
    def state(self) -> LockState:
        """Shortcut for the last acquire result."""
        return self.last_result.state

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _today_lock_id(self) -> str:
        return f"{LOCK_KEY_PREFIX}{self.now_fn().strftime('%Y-%m-%d')}"

    def _get_client(self) -> firestore.Client:
        if self.client is not None:
            return self.client
        # The standard google-cloud-firestore client will honour
        # GOOGLE_APPLICATION_CREDENTIALS (file path) or
        # FIRESTORE_PROJECT_ID + ADC. In the GH Actions workflow we write
        # the service-account JSON to a temp file and set
        # GOOGLE_APPLICATION_CREDENTIALS pointing at it.
        project_id = os.environ.get("FIRESTORE_PROJECT_ID") or None
        self.client = firestore.Client(project=project_id)
        return self.client

    def _persist_state(self, payload: dict[str, Any]) -> None:
        try:
            self.state_file.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            # Failing to write the hand-off file isn't fatal — a sibling
            # ``release`` will fall back to recomputing the lock ID from
            # today's date.
            pass

    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}


# ---------------------------------------------------------------------------
# CLI — invoked from daily-update.sh / .github/workflows/daily-scrape.yml.
# ---------------------------------------------------------------------------


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Acquire or release the daily scrape distributed lock."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    acq = sub.add_parser("acquire", help="Try to take today's lock")
    acq.add_argument(
        "--acquired-by",
        default=os.environ.get("SCRAPE_LOCK_RUNNER", socket.gethostname()),
        help="Runner identifier stored in Firestore. Default: $HOSTNAME.",
    )

    rel = sub.add_parser("release", help="Release today's lock with an outcome")
    rel.add_argument(
        "--outcome",
        default="success",
        choices=["success", "partial", "failed"],
    )
    rel.add_argument(
        "--stats-json",
        default="",
        help="Optional JSON blob persisted alongside the lock for audit.",
    )

    args = parser.parse_args(argv)
    lock = DailyScrapeLock(acquired_by=getattr(args, "acquired_by", socket.gethostname()))

    if args.cmd == "acquire":
        result = lock.acquire()
        # Print one JSON line so a shell wrapper can `jq -r .state`.
        print(json.dumps(result.to_dict()))
        # Exit codes:
        #   0 = ACQUIRED or STOLEN_STALE — caller should run the pipeline
        #   2 = CONTENDED or ALREADY_RUN — caller should exit quietly
        if result.state in (LockState.ACQUIRED, LockState.STOLEN_STALE):
            return 0
        return 2

    if args.cmd == "release":
        stats: dict[str, Any] | None = None
        if args.stats_json:
            try:
                stats = json.loads(args.stats_json)
            except json.JSONDecodeError:
                # Tolerant: write the raw string into a stats blob rather
                # than failing the release path.
                stats = {"_raw": args.stats_json}
        lock.release(outcome=args.outcome, stats=stats)
        print(json.dumps({"released": True, "outcome": args.outcome}))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(_cli())
