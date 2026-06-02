"""Stage-0 dependency / credential preflight for the daily pipeline.

Why this exists (reliability audit 2026-06-01, IL-5 / CID-04 Python side)
========================================================================
The nightly run is all-or-nothing today: if the Firestore service-account key
silently expires (``invalid_grant`` / ``RefreshError``), Stage 0 dead-backfill
and Stage 4 sync both crash, the run is reported degraded, AND the whole night
effectively no-ops for the user-facing matcher even though scrape/enrich/embed
could have proceeded and kept Postgres fresh. The opposite failure — a missing
core secret (DATABASE_URL / ANTHROPIC_API_KEY / OPENAI_API_KEY) — means the run
cannot do anything useful and should abort loudly rather than burn an hour
producing nothing.

This module separates those two cases so the night can DEGRADE instead of
no-op:

  * HARD FAIL (abort the run): a required core secret is missing/unloadable.
  * SOFT DEGRADE (skip ONLY sync, keep scrape/enrich/embed): Firestore
    credentials are configured but not currently usable (expired/revoked SA,
    missing ADC). ``sync_ok`` goes False; the orchestrator sets
    ``skip_sync=True`` and runs everything else so Postgres stays current and
    the next healthy night syncs the backlog.
  * BEST-EFFORT canaries (never gate): optional Firecrawl/Serper key presence.
    A problem is appended to ``problems`` for visibility but never fails or
    degrades the run.

Design rules (so importing this module is always offline-safe):
  * NO network at import time. Google libraries are imported LAZILY inside the
    probe function. ``import wekruit_matching.pipeline.preflight`` must succeed
    with no creds and no network.
  * The live Firestore token-mint RPC (the only call that can detect an
    expired/revoked key) is gated behind the ``WEKRUIT_PREFLIGHT_PROBE_FIRESTORE``
    env flag so unit tests never touch the network. Parsing/loading the SA
    credential object is always done (offline) and catches malformed creds.

CLI (Stage 0 of scripts/daily-update.sh branches on the exit code):
    python -m wekruit_matching.pipeline.preflight
      exit 0 -> all deps live; proceed normally
      exit 2 -> ONLY Firestore/sync credential down; caller should degrade
                (export WEKRUIT_SKIP_SYNC=1, keep scrape/enrich/embed)
      exit 1 -> hard fail (a core secret missing); abort + alert
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field


@dataclass
class PreflightResult:
    """Outcome of a Stage-0 dependency/credential preflight.

    ``ok``        — overall green: no hard fail AND sync is usable.
    ``sync_ok``   — Firestore/sync credentials are usable. When False (and not a
                    hard_fail) the caller should degrade: skip sync, keep the
                    rest of the pipeline.
    ``hard_fail`` — a required core dependency is missing; the run must abort.
    ``problems``  — human-readable diagnostics (best-effort canaries + the
                    reason a probe failed). Never contains secret values.
    """

    ok: bool
    sync_ok: bool
    hard_fail: bool
    problems: list[str] = field(default_factory=list)


# Live token-mint RPC is the only thing that can detect an expired/revoked SA.
# It is a network call, so it is OFF unless explicitly enabled (prod/cron set it
# in daily-update.sh). Offline parsing of the credential object always runs.
_PROBE_FIRESTORE_ENV = "WEKRUIT_PREFLIGHT_PROBE_FIRESTORE"

# Scopes used purely to mint a probe token; identical to what the Firestore
# client would request. No data is read/written by the probe.
_PROBE_SCOPES = ("https://www.googleapis.com/auth/datastore",)


def _check_core_env(problems: list[str]) -> bool:
    """Return True iff the required core secrets are present and loadable.

    Reads via ``get_settings`` (pydantic-settings, the project's documented
    config mechanism — it loads ``.env`` and validates types) rather than bare
    ``os.environ`` so this matches how every other entrypoint resolves config.
    A missing required field raises ``ValidationError`` at ``get_settings()``
    time, which we translate into a hard-fail problem. Empty-string values are
    also treated as missing (a blank secret is unusable).
    """
    try:
        from wekruit_matching.config import get_settings

        settings = get_settings()
    except Exception as e:  # noqa: BLE001 - any config-load failure is a hard fail
        # ValidationError (missing required env) or anything else unloadable.
        problems.append(f"core config unloadable: {type(e).__name__}: {e}")
        return False

    missing: list[str] = []
    for field_name, label in (
        ("database_url", "DATABASE_URL"),
        ("anthropic_api_key", "ANTHROPIC_API_KEY"),
        ("openai_api_key", "OPENAI_API_KEY"),
    ):
        value = getattr(settings, field_name, "") or ""
        if not str(value).strip():
            missing.append(label)
    if missing:
        problems.append(
            "missing required core secret(s): " + ", ".join(missing)
        )
        return False
    return True


def _firestore_creds_configured() -> bool:
    """True iff Firestore creds appear configured (so a probe is meaningful).

    When nothing is configured there is nothing to validate — sync gating
    happens later in the sync stage itself (which requires FIREBASE_SYNC_URL),
    so an unconfigured Firestore is NOT a sync_ok=False signal here.
    """
    try:
        from wekruit_matching.config import get_settings

        settings = get_settings()
        if (getattr(settings, "firebase_service_account_json", "") or "").strip():
            return True
        if (getattr(settings, "firestore_project_id", "") or "").strip():
            return True
    except Exception:  # noqa: BLE001 - core check already reports config errors
        pass
    # Application Default Credentials path (GOOGLE_APPLICATION_CREDENTIALS).
    return bool((os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or "").strip())


def _probe_firestore_credentials(problems: list[str]) -> bool:
    """Return True iff Firestore credentials are usable.

    Two tiers, both with google libs imported LAZILY so module import stays
    offline-safe:

      1. ALWAYS (offline): construct the credential object. A malformed SA JSON
         or absent ADC raises (ValueError / DefaultCredentialsError) and means
         the creds are unusable -> return False.
      2. OPTIONAL (network, gated by WEKRUIT_PREFLIGHT_PROBE_FIRESTORE): mint a
         token via ``credentials.refresh``. This is the only call that detects
         an EXPIRED/REVOKED key (``invalid_grant`` -> ``RefreshError``). A
         failure here -> return False. Disabled by default so unit tests and
         offline runs never hit the network.

    A False return degrades the night (skip sync) — it is NEVER a hard fail.
    """
    try:
        import google.auth
        import google.auth.exceptions as gae
        from google.auth.transport.requests import Request
        from google.oauth2 import service_account
    except Exception as e:  # noqa: BLE001 - google libs unavailable -> degrade
        problems.append(
            f"google auth libraries unavailable for Firestore probe: "
            f"{type(e).__name__}: {e}"
        )
        return False

    # --- Tier 1: construct credentials (offline) ---------------------------
    credentials = None
    try:
        from wekruit_matching.config import get_settings

        sa_json = ""
        try:
            sa_json = (
                getattr(get_settings(), "firebase_service_account_json", "") or ""
            ).strip()
        except Exception:  # noqa: BLE001 - fall through to ADC
            sa_json = ""

        if sa_json:
            import json

            info = json.loads(sa_json)
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=list(_PROBE_SCOPES)
            )
        else:
            # Application Default Credentials (env GOOGLE_APPLICATION_CREDENTIALS
            # or workload identity). google.auth.default() does NOT perform a
            # network call to load the credential object.
            credentials, _project = google.auth.default(scopes=list(_PROBE_SCOPES))
    except (
        ValueError,
        gae.DefaultCredentialsError,
        gae.MalformedError,
    ) as e:
        problems.append(
            f"Firestore credentials malformed/unloadable: {type(e).__name__}: {e}"
        )
        return False
    except Exception as e:  # noqa: BLE001 - any other load failure -> degrade
        problems.append(
            f"Firestore credential load failed: {type(e).__name__}: {e}"
        )
        return False

    # --- Tier 2: live token mint (network, opt-in) -------------------------
    if (os.environ.get(_PROBE_FIRESTORE_ENV) or "").strip() not in ("1", "true", "True"):
        # Network probe disabled: creds parsed fine, treat as usable.
        return True

    try:
        credentials.refresh(Request())
    except gae.RefreshError as e:
        # The expired/revoked-key case (invalid_grant). Degrade, don't abort.
        problems.append(
            f"Firestore token mint failed (expired/revoked credential): "
            f"{type(e).__name__}: {e}"
        )
        return False
    except gae.TransportError as e:
        problems.append(
            f"Firestore token mint transport error: {type(e).__name__}: {e}"
        )
        return False
    except Exception as e:  # noqa: BLE001 - any mint failure -> degrade
        problems.append(
            f"Firestore token mint failed: {type(e).__name__}: {e}"
        )
        return False
    return True


def _canary_optional_keys(problems: list[str]) -> None:
    """Best-effort presence canaries for optional external services.

    Only checks key PRESENCE (never makes a network call here — these are
    Stage-1.7 / Stage-2.5 gated stages that no-op when their key is unset, so a
    missing key is expected, not an error). A configured-but-empty value is the
    only thing worth flagging. NEVER hard-fails, NEVER degrades.
    """
    try:
        from wekruit_matching.config import get_settings

        settings = get_settings()
    except Exception:  # noqa: BLE001 - core check already covers config errors
        return
    # These are intentionally optional; we surface nothing when simply absent.
    # (Left as a hook for future liveness canaries — presence alone is not a
    # problem, so we do not append anything for unset keys.)
    _ = settings


def run_preflight(require_sync: bool = False) -> PreflightResult:
    """Run the Stage-0 dependency/credential preflight.

    Args:
        require_sync: when True, a Firestore/sync credential problem is escalated
            to a HARD FAIL (the caller insists sync must run). Default False:
            a sync-credential problem only degrades (skip sync), so the night
            still refreshes Postgres.

    Returns a :class:`PreflightResult`. No exceptions escape — every failure is
    captured into the result so the caller can branch deterministically.
    """
    problems: list[str] = []

    core_ok = _check_core_env(problems)
    if not core_ok:
        # A missing core secret means the run cannot do anything useful.
        return PreflightResult(
            ok=False, sync_ok=False, hard_fail=True, problems=problems
        )

    # Firestore credential liveness — only meaningful if creds are configured.
    if _firestore_creds_configured():
        sync_ok = _probe_firestore_credentials(problems)
    else:
        # Nothing configured to validate; the sync stage itself gates on
        # FIREBASE_SYNC_URL. Not a degrade signal here.
        sync_ok = True

    # Optional best-effort canaries (never gate).
    _canary_optional_keys(problems)

    if not sync_ok and require_sync:
        # Caller insists sync must run -> a sync-credential problem is fatal.
        return PreflightResult(
            ok=False, sync_ok=False, hard_fail=True, problems=problems
        )

    return PreflightResult(
        ok=sync_ok,
        sync_ok=sync_ok,
        hard_fail=False,
        problems=problems,
    )


def _summary(result: PreflightResult) -> str:
    lines = [
        f"preflight: ok={result.ok} sync_ok={result.sync_ok} "
        f"hard_fail={result.hard_fail}",
    ]
    if result.problems:
        lines.append("problems:")
        for p in result.problems:
            lines.append(f"  - {p}")
    else:
        lines.append("problems: none")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Prints a human summary to stderr and returns the exit
    code daily-update.sh branches on:

        0 = all deps live
        2 = ONLY Firestore/sync credential down (degrade: skip sync)
        1 = hard fail (abort)
    """
    require_sync = bool(argv) and "--require-sync" in (argv or [])
    result = run_preflight(require_sync=require_sync)
    print(_summary(result), file=sys.stderr)
    if result.hard_fail:
        return 1
    if not result.sync_ok:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
