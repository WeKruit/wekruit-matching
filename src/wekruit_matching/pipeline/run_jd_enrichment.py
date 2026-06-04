"""Stage 2b orchestrator for ATS JD enrichment.

Two-clause SELECT gating (P7-F, 2026-05-08):

  Clause 1 (entry): ``jd_fetch_attempted_at IS NULL``
                    OR (``jd_fetch_source = 'failed'``
                        AND COALESCE(permanent_404, FALSE) = FALSE
                        AND ``jd_fetch_attempted_at < NOW() - 7d``)

  Clause 2 (data gap): ``job_description IS NULL OR job_description = ''``

Both must hold. Successfully-fetched jobs (have JD) never re-enter regardless
of age. Permanent-404 jobs (employer pulled the listing) are excluded entirely.
Recoverable failures (Firecrawl down, Workday 5xx, connection timeout) become
eligible after STAGE2B_STALE_DAYS days, giving upstream services time to
recover before we re-spend a fetch.

PARALLELISM (P7-M2, 2026-05-09):
The previous sequential `for row in rows` loop ran ~4.6s/job (Firecrawl HTTP +
Workday CXS discovery + ATS API). With 5K+ jobs eligible after the P7-L URL
fix, sequential drain = 14 daily runs. We now fan out fetches across a
ThreadPoolExecutor (default 10 workers). Each worker grabs its OWN connection
from the psycopg pool (psycopg connections are NOT thread-safe), runs
_fetch_for_url + UPDATE in isolation, and commits independently. Wall-time
target: 10x reduction.

Connection-pool note: get_pool() is configured with max_size=20, so 10 workers
+ the main-thread reader connection stays well under the cap. Mirror of P7-A's
pattern at Stage 2c (worker.py).

Why a 7-day staleness window: short enough that real outages (Firecrawl was
down 5+ weeks in early-2026 — a transient like that needs slack to recover)
don't burn through retry attempts in one day; long enough that we don't burn
LLM credits weekly on permanently-empty rows. Tunable via STAGE2B_STALE_DAYS
module constant; mirror of P7-E's ENRICH_STALE_DAYS at Stage 2c.

Why a boolean ``permanent_404`` rather than a 3-value enum: additive boolean
is a cheaper migration (one column, defaults FALSE) and ``success`` is
already implicit in row state (``job_description`` populated, ``jd_fetch_source``
non-failed). An enum would duplicate that signal. NULL-safety via
COALESCE(permanent_404, FALSE) handles any rows missed by the default
backfill.
"""
from __future__ import annotations

import asyncio
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import urlparse

import httpx
from loguru import logger

from wekruit_matching.config import get_settings
from wekruit_matching.db.connection import get_connection
from wekruit_matching.enrichment.readiness import jd_usable
from wekruit_matching.pipeline.ats_enricher import (
    AtsJobData,
    fetch_ashby_job,
    fetch_greenhouse_job,
    fetch_jobright_job,
    fetch_lever_job,
)
from wekruit_matching.pipeline.firecrawl_enricher import (
    ClosedAtSourceError,
    fetch_firecrawl_job,
    fetch_workday_job,
    search_canonical_job_url,
)
from wekruit_matching.pipeline.url_classifier import FetchRoute, classify_job_url, normalize_job_url

# Re-attempt window for *recoverable* Stage 2b failures.
#
# Lowered 7 → 1 (2026-05-20, matching-quality launch blocker):
# the 6,888 NULL-JD active-pool backlog at launch eve was bottlenecked by the
# stale window. With STAGE2B_STALE_DAYS=1, transient Firecrawl/Workday/Lever
# 5xx outages get one retry per day instead of one per week, draining the
# backlog far faster. ``permanent_404=TRUE`` rows are still excluded forever,
# so the lowered window only re-enters recoverable failures.
STAGE2B_STALE_DAYS = 1


_AGGREGATOR_HOSTS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "simplyhired.com",
)


def _is_aggregator_url(url: str) -> bool:
    hostname = urlparse(normalize_job_url(url)).netloc.lower()
    return any(host in hostname for host in _AGGREGATOR_HOSTS)


def _is_permanent_404(exc: BaseException) -> bool:
    """Return True ONLY for a PROVABLY-gone URL (real HTTP 404 / 410 Gone).

    ``permanent_404=TRUE`` is load-bearing in two destructive ways: the Stage 2b
    SELECT excludes the row from JD retry FOREVER (no stale-window, unlike the
    ``'failed'`` sentinel), AND ``reconcile_dead_inactive()`` flips it
    active->inactive — removed from the live matcher. So it must be reserved for
    actual proof of permanence.

    2026-06-04 poison_no_retry fix — the following were tombstoning LIVE jobs on
    TRANSIENT hiccups and are now treated as recoverable (``jd_fetch_source=
    'failed'``, which the Stage 2b SELECT re-admits after the stale window):
      * ``LookupError`` — raised when the Workday CXS endpoint hasn't rendered
        yet, or an Ashby posting is absent from a transiently-empty/paginated
        board feed. Transient, not gone.
      * HTTP 403 from anti-bot aggregator hosts — the posting is NOT gone, the
        bot was blocked. Tombstoning it inactivates a live job. (Periodic
        re-fetch after the stale window costs a little budget; that is the right
        trade vs. yanking a real posting out of the matcher.)
    """
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            status = exc.response.status_code
        except AttributeError:
            return False
        if status in (404, 410):
            return True
    return False


def _throttle_domain(
    last_request_at: dict[str, float],
    domain: str,
    *,
    min_interval_seconds: float,
    lock: threading.Lock | None = None,
) -> None:
    """Ensure requests to the same domain are spaced out.

    Thread-safe variant: when `lock` is provided (parallel mode), the read +
    sleep + write of last_request_at[domain] runs under the lock so two
    workers can't both observe "no recent request" and both fire instantly.
    """
    if not domain or min_interval_seconds <= 0:
        return

    if lock is not None:
        with lock:
            now = time.monotonic()
            previous = last_request_at.get(domain)
            if previous is not None:
                remaining = min_interval_seconds - (now - previous)
                if remaining > 0:
                    time.sleep(remaining)
            last_request_at[domain] = time.monotonic()
        return

    now = time.monotonic()
    previous = last_request_at.get(domain)
    if previous is not None:
        remaining = min_interval_seconds - (now - previous)
        if remaining > 0:
            time.sleep(remaining)
    last_request_at[domain] = time.monotonic()


async def _search_url(row: dict, settings) -> str | None:
    if not settings.firecrawl_api_key:
        return None
    return await search_canonical_job_url(
        company_name=row["company_name"],
        role_title=row["role_title"],
        api_key=settings.firecrawl_api_key,
        base_url=settings.firecrawl_base_url,
    )


def _jd_is_thin(data: AtsJobData | None) -> bool:
    """True when an ATS-API fetch came back with no usable JD body.

    Some ATS public APIs (observed: Lever for Spotify/Ledger/Voltus, Ashby for
    certain boards) return 200 with metadata but an EMPTY description_plain — the
    JD only lives in the JS-rendered page. In that case the structured API path
    yields a 0-length JD that fails the >=200 matchable gate, so the job never
    becomes matchable. We treat that as "thin" and fall back to Firecrawl, which
    renders the page and extracts the real JD.
    """
    if data is None:
        return True
    # Shared readiness definition (rank 3): "thin" == not a usable JD.
    return not jd_usable(getattr(data, "description_plain", None))


async def _fetch_for_url(url: str, settings) -> tuple[AtsJobData | None, int]:
    classification = classify_job_url(url)
    route = classification.route

    # Structured ATS-API routes first (cheap, no Firecrawl credits). If the API
    # returns an empty/thin JD (the page renders the JD client-side), fall back
    # to Firecrawl extract — the URL is valid, the API just doesn't expose the
    # body. Verified 2026-05-31: lever.co/spotify/... API JD len=0 but Firecrawl
    # extract returns the full ~2.5k-char JD.
    if route in (FetchRoute.GREENHOUSE, FetchRoute.LEVER, FetchRoute.ASHBY):
        if route is FetchRoute.GREENHOUSE:
            api_data = fetch_greenhouse_job(url)
        elif route is FetchRoute.LEVER:
            api_data = fetch_lever_job(url)
        else:
            api_data = fetch_ashby_job(url)
        if not _jd_is_thin(api_data):
            return api_data, 0
        # API JD was empty/thin — escalate to Firecrawl if available.
        if not settings.firecrawl_api_key:
            return api_data, 0  # nothing better available; let caller handle thin
        firecrawl = await fetch_firecrawl_job(
            url,
            api_key=settings.firecrawl_api_key,
            base_url=settings.firecrawl_base_url,
        )
        if firecrawl and not _jd_is_thin(firecrawl.job_data):
            return firecrawl.job_data, firecrawl.credits_used
        # Firecrawl also thin/failed — return whichever we had (API data keeps
        # any metadata; caller's >=200 gate will mark it not-matchable honestly).
        return api_data, (firecrawl.credits_used if firecrawl else 0)
    if route is FetchRoute.JOBRIGHT:
        # 2026-05-21: jobright.ai /jobs/info/<id> pages are server-rendered
        # with the full JD inline — no Firecrawl needed. Direct HTTP fetch
        # + text-strip is ~10x cheaper than Firecrawl credits and avoids
        # the SPA-wait-for-render dance entirely.
        return fetch_jobright_job(url), 0
    if route is FetchRoute.WORKDAY:
        if not settings.firecrawl_api_key:
            return None, 0
        firecrawl = await fetch_firecrawl_job(
            url,
            api_key=settings.firecrawl_api_key,
            base_url=settings.firecrawl_base_url,
        )
        return (firecrawl.job_data, firecrawl.credits_used) if firecrawl else (None, 0)
    if not settings.firecrawl_api_key:
        return None, 0
    firecrawl = await fetch_firecrawl_job(
        url,
        api_key=settings.firecrawl_api_key,
        base_url=settings.firecrawl_base_url,
    )
    return (firecrawl.job_data, firecrawl.credits_used) if firecrawl else (None, 0)


def _write_success(conn, job_id: str, result: AtsJobData) -> None:
    # 2026-06-02 fix: landing a usable JD on a row that was previously stamped
    # enriched_at with EMPTY skills (legal under the no-JD floor exception of
    # ck_enriched_requires_skills_or_no_jd) would push it into the violating
    # state "enriched_at NOT NULL + JD>=200 + skills empty" and the INSERT/UPDATE
    # would be REJECTED by the constraint (observed live: 2,200 rejections +
    # poisoned batch txns in the first daily run after alembic 0010). A row that
    # just got a JD but has no skills genuinely needs (re-)enrichment, so clear
    # enriched_at here when skills are still empty — the Stage 2c gap-fill
    # enricher then extracts skills and re-stamps. (When skills already exist the
    # COALESCE leaves enriched_at untouched.)
    conn.execute(
        """
        UPDATE jobs
        SET
          job_description = %(job_description)s,
          core_responsibilities = %(core_responsibilities)s,
          qualifications = %(qualifications)s,
          benefits = %(benefits)s,
          salary_range = %(salary_range)s,
          data_quality_score = %(data_quality_score)s,
          ats_content_hash = %(ats_content_hash)s,
          jd_fetch_source = %(jd_fetch_source)s,
          jd_fetch_attempted_at = NOW(),
          permanent_404 = FALSE,
          enriched_at = CASE
              WHEN required_skills IS NULL OR cardinality(required_skills) = 0
              THEN NULL
              ELSE enriched_at
          END
        WHERE job_id = %(job_id)s
        """,
        {
            "job_id": job_id,
            "job_description": result.description_plain,
            "core_responsibilities": result.core_responsibilities,
            "qualifications": result.qualifications,
            "benefits": result.benefits,
            "salary_range": result.salary_range,
            "data_quality_score": result.data_quality_score,
            "ats_content_hash": sha256(result.description_plain.encode("utf-8")).hexdigest(),
            "jd_fetch_source": result.source,
        },
    )


def _write_skip_no_url(conn, job_id: str) -> None:
    """Mark a row as 'no fetchable URL' so it leaves the active queue.

    Written when ``_pick_target_url`` returns None — both ``primary_url`` and
    ``ats_apply_url`` were jobright-only or empty. We set ``jd_fetch_source``
    to a distinct sentinel ('skip_no_url') so the queue-selection clause
    excludes it (it only re-admits rows where ``jd_fetch_source = 'failed'``).
    Tracks C/F can clear this column when a real ATS URL is backfilled, and
    the row will re-enter on the next run.

    ``permanent_404`` stays FALSE: the row is not permanently dead, just
    presently unfetchable from this pipeline. The jobright-native enricher
    (``enrich_from_jobright.py``) is the proper path for these.
    """
    conn.execute(
        """
        UPDATE jobs
        SET
          jd_fetch_source = 'skip_no_url',
          jd_fetch_attempted_at = NOW(),
          permanent_404 = FALSE
        WHERE job_id = %(job_id)s
        """,
        {"job_id": job_id},
    )


def _write_failure(conn, job_id: str, *, permanent_404: bool = False) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET
          jd_fetch_source = %(jd_fetch_source)s,
          jd_fetch_attempted_at = NOW(),
          permanent_404 = %(permanent_404)s
        WHERE job_id = %(job_id)s
        """,
        {
            "job_id": job_id,
            "jd_fetch_source": "failed",
            "permanent_404": permanent_404,
        },
    )


def _write_closed_at_source(conn, job_id: str) -> None:
    """Tombstone a row whose ATS page returned 200 + closed-marker body.

    The row is treated as permanently dead (``permanent_404=TRUE``) so the
    Stage 2b queue selection clause never re-admits it, even after the
    STAGE2B_STALE_DAYS recovery window. ``jd_fetch_source='closed_at_source'``
    is a distinct sentinel so ops dashboards can tell ATS-closed tombstones
    apart from real 404 tombstones.

    Idempotent: re-calling on an already-tombstoned row writes the same
    fields and bumps ``jd_fetch_attempted_at`` — no double-count, no flip
    of ``status``.
    """
    conn.execute(
        """
        UPDATE jobs
        SET
          jd_fetch_source = 'closed_at_source',
          jd_fetch_attempted_at = NOW(),
          permanent_404 = TRUE
        WHERE job_id = %(job_id)s
        """,
        {"job_id": job_id},
    )


def _pick_target_url(row: dict) -> str | None:
    """Pick the URL most likely to yield a JD.

    Priority:
      1. ``ats_apply_url`` when present and non-jobright (real ATS = best JD source)
      2. ``primary_url`` when non-jobright (direct ATS already)
      3. ``primary_url`` when jobright (2026-05-21: jobright /jobs/info/<id>
         pages are server-rendered with full JD inline — see
         ``fetch_jobright_job`` for the dedicated parser).
      4. ``ats_apply_url`` as a last resort even if it is jobright-shaped.

    Returns None only when both URLs are missing — in that case the row
    truly has nothing to fetch from. The previous implementation returned
    None when primary was jobright and ats was absent, which masked the
    JD in 2,308 jobright-newgrad active docs (5.6% JD coverage).
    """
    primary_url_raw = row.get("primary_url") or ""
    ats_apply_url_raw = row.get("ats_apply_url") or ""
    primary_is_jobright = primary_url_raw.startswith("https://jobright.ai/")
    ats_is_real = bool(ats_apply_url_raw) and not ats_apply_url_raw.startswith(
        "https://jobright.ai/"
    )
    if ats_is_real:
        return ats_apply_url_raw
    if primary_url_raw and not primary_is_jobright:
        return primary_url_raw
    if primary_is_jobright:
        return primary_url_raw
    if ats_apply_url_raw:
        return ats_apply_url_raw
    return None


@contextmanager
def _default_connection_factory():
    """Default per-worker connection factory: pool-acquired psycopg conn."""
    with get_connection() as conn:
        yield conn


def _process_one_job(
    row: dict,
    *,
    settings,
    domain_min_interval: float,
    last_request_at: dict[str, float],
    domain_lock: threading.Lock,
    dry_run: bool,
    write_conn,
) -> dict:
    """Process one job: classify URL, fetch JD, persist.

    Pure worker payload — never raises. Returns a dict with counters the
    caller aggregates under counter_lock.

    `write_conn` is the psycopg connection to use for UPDATE + commit. In
    parallel mode this is a per-worker pooled connection; in sequential
    mode (max_workers=1) it is the caller's conn (no per-job commit so
    caller can batch).
    """
    out = {
        "processed": 0,
        "failed": 0,
        "skipped": 0,
        "credits_used": 0,
        "route": None,  # FetchRoute.value or None
        "failed_route": None,
    }

    original_url = _pick_target_url(row)
    if original_url is None:
        # No fetchable URL (typically jobright-only). Mark the row so it leaves
        # the queue — without this the row re-enters every run forever once the
        # SQL filter no longer excludes jobright-only docs (see SELECT clause).
        out["skipped"] = 1
        if not dry_run:
            try:
                _write_skip_no_url(write_conn, row["job_id"])
            except Exception as write_exc:
                logger.warning(
                    "Could not persist skip_no_url for {}: {}",
                    row["job_id"],
                    write_exc,
                )
        return out

    target_url = original_url
    route = classify_job_url(original_url).route
    if _is_aggregator_url(original_url):
        try:
            resolved = asyncio.run(_search_url(row, settings))
        except Exception as exc:
            logger.warning("Aggregator URL search failed for {}: {}", row["job_id"], exc)
            resolved = None
        if resolved:
            target_url = resolved
            route = classify_job_url(target_url).route

    out["route"] = route.value
    out["processed"] = 1

    if dry_run:
        return out

    domain = urlparse(normalize_job_url(target_url)).netloc.lower()
    _throttle_domain(
        last_request_at,
        domain,
        min_interval_seconds=domain_min_interval,
        lock=domain_lock,
    )

    try:
        result, spend = asyncio.run(_fetch_for_url(target_url, settings))
        out["credits_used"] = spend
        if result is None:
            if route is FetchRoute.FIRECRAWL and not settings.firecrawl_api_key:
                out["processed"] = 0
                out["skipped"] = 1
                return out
            _write_failure(write_conn, row["job_id"], permanent_404=False)
            out["failed_route"] = route.value
            out["failed"] = 1
            return out
        # rank-5 fix: a non-None result can still carry a thin/empty JD (both the
        # ATS API and the Firecrawl fallback came back short). Writing
        # _write_success would stamp a real jd_fetch_source (= "done") on a body
        # that fails the >=200 matchable gate -> the row is "fetched" but never
        # "usable", never re-fetched (Stage 2b re-admits only source='failed'),
        # and never embeds. Treat a thin result as a FAILURE so it stays
        # jd_fetch_source='failed' and re-enters Stage 2b after the stale window.
        if not jd_usable(getattr(result, "description_plain", None)):
            _write_failure(write_conn, row["job_id"], permanent_404=False)
            out["failed_route"] = route.value
            out["failed"] = 1
            return out
        _write_success(write_conn, row["job_id"], result)
    except ClosedAtSourceError as exc:
        # ATS page returned 200 + body says "closed". Tombstone with
        # permanent_404=TRUE so we never re-spend a Firecrawl credit on it.
        # Logged at INFO not WARNING — this is the system working correctly,
        # not a fault. The matched marker is preserved for ops curation.
        logger.info(
            "JD enrichment closed-at-source for {} (url={} marker={!r})",
            row["job_id"],
            exc.url,
            exc.matched_marker,
        )
        try:
            _write_closed_at_source(write_conn, row["job_id"])
        except Exception as write_exc:
            logger.warning(
                "Could not persist closed_at_source tombstone for {}: {}",
                row["job_id"],
                write_exc,
            )
        out["failed_route"] = route.value
        out["failed"] = 1
    except Exception as exc:
        permanent = _is_permanent_404(exc)
        logger.warning(
            "JD enrichment failed for {} (permanent_404={}): {}",
            row["job_id"],
            permanent,
            exc,
        )
        # 2026-06-02: if the failure was a DB error (e.g. a CHECK violation from
        # _write_success), psycopg has marked this txn ABORTED — any further
        # statement raises "current transaction is aborted". Roll back FIRST so
        # the recovery _write_failure runs in a clean transaction instead of
        # silently no-op'ing (which previously left the row unmarked AND could
        # poison the next sibling sharing this conn).
        try:
            write_conn.rollback()
        except Exception:  # noqa: BLE001 — best-effort reset
            pass
        try:
            _write_failure(write_conn, row["job_id"], permanent_404=permanent)
            write_conn.commit()
        except Exception as write_exc:  # defensive — don't crash sibling workers
            logger.warning("Could not persist failure for {}: {}", row["job_id"], write_exc)
            try:
                write_conn.rollback()
            except Exception:  # noqa: BLE001
                pass
        out["failed_route"] = route.value
        out["failed"] = 1

    return out


def run_jd_enrichment(
    *,
    conn=None,
    settings=None,
    batch_size: int = 500,
    domain_min_interval: float = 0.5,
    dry_run: bool = False,
    # 2026-05-20 user goal: default raised 1 → 3.
    # Justification: 1 was a safety pin while the signal-timeout bug (loop
    # tick #5 finding) blocked parallel default. 3 trades a bounded 3x
    # throughput gain (~14k-doc backlog drains in ~3 days instead of ~9)
    # against the same signal-timeout exposure as max_workers=10. Worth it
    # because the bug is intermittent, not catastrophic — workers crash
    # individually, sibling workers continue (per
    # ``test_parallel_per_job_error_isolation``). Re-raise to 10 once the
    # signal-timeout fix lands. Idempotency holds: per-row UPDATEs are
    # WHERE job_id = ?, so parallel writers don't step on each other —
    # the same row processed twice yields the same final state.
    max_workers: int = 3,
    connection_factory=None,
) -> dict:
    """Process the JD enrichment queue in batches of at most 500 rows.

    Concurrency (P7-M2):
      - max_workers > 1: parallel mode. Each worker acquires its own pooled
        psycopg connection via `connection_factory` (defaults to get_connection),
        runs _fetch_for_url + UPDATE, and commits independently. The caller's
        `conn` is used only for the SELECT query.
      - max_workers == 1: sequential mode. Writes go through `conn`; caller's
        commit batches per-page. Preserves legacy semantics for tests.

    Args:
        conn: psycopg connection used for the SELECT and (in sequential mode)
            UPDATE. Workers in parallel mode do NOT share this connection.
        settings: optional override for get_settings().
        batch_size: max rows per SELECT page (capped at 500).
        domain_min_interval: minimum seconds between fetches against the same
            host. Throttle is shared across workers via a lock.
        dry_run: when True, classify routes and count work but skip network +
            DB writes.
        max_workers: ThreadPoolExecutor size. Default 10. Set to 1 for
            sequential behaviour (tests, debugging).
        connection_factory: context-manager factory yielding a psycopg
            connection for worker writes. Defaults to get_connection. Tests
            inject a fake here.
    """
    if conn is None:
        with get_connection() as owned_conn:
            return run_jd_enrichment(
                conn=owned_conn,
                settings=settings,
                batch_size=batch_size,
                domain_min_interval=domain_min_interval,
                dry_run=dry_run,
                max_workers=max_workers,
                connection_factory=connection_factory,
            )

    settings = settings or get_settings()
    if not hasattr(settings, "firecrawl_api_key"):
        settings = SimpleNamespace(
            firecrawl_api_key="",
            firecrawl_base_url="https://api.firecrawl.dev",
            **settings.__dict__,
        )

    if connection_factory is None:
        connection_factory = _default_connection_factory

    source_counts: dict[str, int] = defaultdict(int)
    failed_by_source: dict[str, int] = defaultdict(int)
    processed = failed = skipped = credits_used = 0
    last_request_at: dict[str, float] = {}
    domain_lock = threading.Lock()
    counter_lock = threading.Lock()
    # Cap raised 500 → 5000 (2026-05-20, matching-quality launch blocker):
    # at the 6,888-doc backlog, a 500-row cap forced 14 paginated SELECTs per
    # run with the same per-row work in sequential mode. 5000 in-memory rows
    # is cheap; the cost is per-fetch network/Firecrawl latency, not the page.
    # When parallelism (max_workers > 1) is re-enabled the cap unlocks
    # 50K-attempt runs (10 workers × 5K page).
    effective_batch_size = min(batch_size, 5000)
    sequential = max_workers <= 1

    def _aggregate(out: dict) -> None:
        nonlocal processed, failed, skipped, credits_used
        with counter_lock:
            processed += out["processed"]
            failed += out["failed"]
            skipped += out["skipped"]
            credits_used += out["credits_used"]
            if out["route"] is not None and out["processed"]:
                source_counts[out["route"]] += 1
            if out["failed_route"] is not None:
                failed_by_source[out["failed_route"]] += 1

    while True:
        rows = conn.execute(
            f"""
            SELECT job_id, company_name, role_title, primary_url, ats_apply_url
            FROM jobs
            WHERE status = 'active'
              -- rank-6 fix: admit any row WITHOUT a usable JD body — NULL, empty,
              -- OR thin (<200). The old `IS NULL OR = ''` clause excluded a
              -- 1..199-char JD, so a row stamped with a real source name on a
              -- thin body (the rank-5 class, pre-fix) could never re-enter and
              -- never embed. Mirrors readiness.jd_usable's >=200 floor.
              AND (job_description IS NULL OR length(job_description) < 200)
              -- Pre-filter dropped 2026-05-20 (matching-quality launch blocker):
              -- the prior ``primary_url NOT LIKE jobright OR ats_apply_url NOT
              -- LIKE jobright`` clause silently hid jobright-only rows from
              -- every metric so the 6,888 NULL-JD backlog showed up as "no
              -- pending work". Now every active thin-JD row is admitted; the
              -- _pick_target_url skip-path marks no-fetchable-URL rows with
              -- ``jd_fetch_source='skip_no_url'`` so they leave the queue
              -- after one pass (still re-enter when an ATS URL is backfilled
              -- and the source column is cleared by Track C / F).
              AND (
                jd_fetch_attempted_at IS NULL
                OR (
                  -- Re-admit a stale prior attempt for recovery. rank-6: this
                  -- now covers BOTH 'failed' AND a real-source stamp left on a
                  -- thin body (the row still has length<200 per the clause
                  -- above), so already-stamped thin rows are not stuck forever.
                  jd_fetch_source <> 'skip_no_url'
                  AND jd_fetch_source <> 'closed_at_source'
                  AND COALESCE(permanent_404, FALSE) = FALSE
                  AND jd_fetch_attempted_at < NOW() - INTERVAL '{STAGE2B_STALE_DAYS} days'
                )
              )
            ORDER BY first_seen_at DESC
            LIMIT %(limit)s
            """,
            {"limit": effective_batch_size},
        ).fetchall()
        if not rows:
            break

        if sequential:
            # Legacy synchronous path — writes go to the caller's conn so
            # existing tests can assert on conn.executed without injecting
            # a connection factory.
            for row in rows:
                out = _process_one_job(
                    row,
                    settings=settings,
                    domain_min_interval=domain_min_interval,
                    last_request_at=last_request_at,
                    domain_lock=domain_lock,
                    dry_run=dry_run,
                    write_conn=conn,
                )
                _aggregate(out)
            if not dry_run:
                conn.commit()
            continue

        # Parallel path: fan out across max_workers, each with its own conn.
        def _worker(row):
            if dry_run:
                # Dry-run never writes; bypass connection acquisition.
                return _process_one_job(
                    row,
                    settings=settings,
                    domain_min_interval=domain_min_interval,
                    last_request_at=last_request_at,
                    domain_lock=domain_lock,
                    dry_run=True,
                    write_conn=None,
                )
            try:
                with connection_factory() as worker_conn:
                    result = _process_one_job(
                        row,
                        settings=settings,
                        domain_min_interval=domain_min_interval,
                        last_request_at=last_request_at,
                        domain_lock=domain_lock,
                        dry_run=False,
                        write_conn=worker_conn,
                    )
                    worker_conn.commit()
                    return result
            except Exception as exc:  # connection-acquisition failure
                logger.warning("Worker connection error for {}: {}", row.get("job_id"), exc)
                return {
                    "processed": 0,
                    "failed": 1,
                    "skipped": 0,
                    "credits_used": 0,
                    "route": None,
                    "failed_route": "connection_error",
                }

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_worker, row) for row in rows]
            for fut in as_completed(futures):
                try:
                    out = fut.result()
                except Exception as inner_exc:  # _worker should never raise, defensive
                    logger.warning("Worker raised unexpectedly: {}", inner_exc)
                    with counter_lock:
                        failed += 1
                    continue
                _aggregate(out)

    return {
        "processed": processed,
        "failed": failed,
        "skipped": skipped,
        "credits_used": credits_used,
        "sources": dict(source_counts),
        "failed_by_source": dict(failed_by_source),
        "dry_run": dry_run,
    }
