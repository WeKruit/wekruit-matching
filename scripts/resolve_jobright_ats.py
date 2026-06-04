"""Resolve direct ATS apply URLs for jobright jobs via Serper.dev search.

WHY: Phase 66 (commit b81ecaf) deleted the Serper URL resolver and pointed at a
wekruit-pa CF that was never built, so ~19.7k jobright jobs have no direct
ats_apply_url — users get jobright.ai redirect links. Measured Serper hit rate
on a random jobright sample = 87% direct ATS. This restores resolution,
jobright-only per the user's directive.

APPROACH (mirrors the deleted url_resolver.resolve_via_serper, self-contained):
  For each active jobright job missing ats_apply_url:
    1. Pass 1 (exact):  '"{role_title}" "{company_name}" careers apply'
    2. Pass 2 (broad):  '{role_title} {company_name} apply careers'  (if pass 1 misses)
    Pick best organic result: official employer/ATS > aggregator; skip
    jobright.ai / simplify.jobs. Optionally HEAD-verify alive. Write
    ats_apply_url (the resolver finds a URL, it does NOT fetch a JD, so it does
    NOT stamp a jd_fetch_source on a hit). On no-match, stamp
    jd_fetch_source='skip_no_url' (a constraint-legal "no URL" sentinel) so
    reruns skip it.

SAFE:
  - --dry-run prints what it WOULD resolve, zero writes / zero Serper cost.
  - --limit N for a bounded paid run (validate before full ~19.7k).
  - Batched DB writes (executemany per batch), commit per batch.
  - Reversible: every (job_id, old=NULL, new_url) appended to
    data/jobright_ats_resolved.tsv. To revert: set ats_apply_url=NULL for those.
  - 0.3s throttle between Serper calls; tenacity-free simple retry on 429.
  - Idempotent: WHERE ats_apply_url IS NULL AND jd_fetch_source NOT IN
    ('skip_no_url','serper_miss')  ('serper_miss' = legacy pre-2026-06-03 misses).

    uv run python scripts/resolve_jobright_ats.py --dry-run --limit 20
    uv run python scripts/resolve_jobright_ats.py --limit 200        # paid sample
    uv run python scripts/resolve_jobright_ats.py                    # full run
"""
from __future__ import annotations

import argparse
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from loguru import logger

from wekruit_matching.config import get_settings
from wekruit_matching.db.connection import get_connection

_SERPER_URL = "https://google.serper.dev/search"
_SKIP_DOMAINS = ("jobright.ai", "simplify.jobs")
_AGGREGATOR_DOMAINS = (
    "linkedin.com", "glassdoor.com", "indeed.com", "ziprecruiter.com",
    "lensa.com", "builtin.com", "wayup.com", "wellfound.com", "monster.com",
    "talent.com", "jobilize.com", "salary.com", "careerbuilder.com",
    "dice.com", "simplyhired.com", "bebee.com", "theirstack.com",
)
_RESOLVED_LOG = Path("data/jobright_ats_resolved.tsv")
_THROTTLE_S = 0.3

# HTTP statuses that mean "the dependency is DOWN / misconfigured", not "no
# result". Serper returns 400 with body {"message":"Not enough credits"} when the
# account balance is zero; 401/403 = bad/revoked key; 402 = payment required.
# Retrying these is futile — they require a human (top up credits / rotate key).
# Swallowing them as an empty result is exactly the bug that hid a dead Serper
# for days (resolved=0, errors=0, status ok, zero alerts).
_SERPER_INFRA_STATUSES = frozenset({400, 401, 402, 403})


class SerperInfraError(RuntimeError):
    """Serper signalled an infrastructure failure (auth/credit/quota or a
    persistent 429/5xx/network), NOT a 'no results' answer.

    The caller MUST treat this as the dependency being DOWN: abort the run, alert
    a human, and do NOT write miss-sentinels. A row that was never truly queried
    must stay eligible for retry — poisoning it as ``skip_no_url`` would block
    re-resolution even after credits are topped up."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _classify(url: str) -> tuple[int, str]:
    u = url.lower()
    if any(d in u for d in _SKIP_DOMAINS):
        return -1, "skip"
    for agg in _AGGREGATOR_DOMAINS:
        if agg in u:
            return 2, f"serper_{agg.split('.')[0]}"
    return 1, "serper"


def _verify_alive(client: httpx.Client, url: str) -> bool:
    try:
        r = client.head(url, follow_redirects=True, timeout=5.0)
        if r.status_code == 405:
            r = client.get(url, follow_redirects=True, timeout=5.0)
        return r.status_code < 400
    except Exception:
        return True  # assume alive on error — don't drop a valid URL


def _best_url(organic: list[dict], client: httpx.Client, verify: bool) -> tuple[str | None, str]:
    cands: list[tuple[int, str, str]] = []
    for res in organic:
        link = res.get("link") or ""
        if not link:
            continue
        pri, src = _classify(link)
        if pri < 0:
            continue
        cands.append((pri, src, link))
    cands.sort(key=lambda x: x[0])
    for _pri, src, url in cands:
        if not verify or _verify_alive(client, url):
            return url, src
    return None, "none"


def _serper(client: httpx.Client, key: str, q: str) -> list[dict]:
    """Run one Serper search. Returns the ``organic`` list — possibly empty, which
    is a GENUINE no-result miss. Raises :class:`SerperInfraError` when Serper
    signals an infrastructure failure (auth/credit/quota, or a 429/5xx/network
    error that persists across retries) so the caller can tell a DOWN dependency
    apart from an empty result instead of silently treating both as ``[]``."""
    last_detail = "unknown error"
    for attempt in range(3):
        try:
            r = client.post(
                _SERPER_URL,
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": q, "num": 6},
                timeout=15.0,
            )
            if r.status_code == 200:
                return r.json().get("organic", [])
            if r.status_code in _SERPER_INFRA_STATUSES:
                # Auth/credit/quota: human action required. Do NOT retry, do NOT
                # swallow. Surface the body so the alert names the cause (e.g.
                # "Not enough credits"). Never include the key — body has none.
                detail = r.text[:200].replace("\n", " ").strip()
                raise SerperInfraError(
                    f"HTTP {r.status_code}: {detail}", status_code=r.status_code
                )
            if r.status_code == 429:
                last_detail = "HTTP 429 rate limited"
                time.sleep(2 * (attempt + 1))
                continue
            # Unexpected 5xx/other: retry, then escalate to infra if persistent.
            last_detail = f"HTTP {r.status_code}: {r.text[:120].strip()}"
            time.sleep(1)
        except SerperInfraError:
            raise
        except Exception as exc:  # network/timeout — retry, then escalate
            last_detail = f"{type(exc).__name__}: {exc}"
            time.sleep(1)
    # Retries exhausted on a transient class → the dependency is effectively down
    # right now. Escalate instead of returning [] (the old silent-swallow bug).
    raise SerperInfraError(last_detail)


def resolve_jobright_pending(
    *,
    limit: int | None = None,
    workers: int = 16,
    dry_run: bool = False,
    verify: bool = True,
) -> dict[str, object]:
    """Resolve direct ATS apply URLs for pending jobright jobs via Serper.

    Importable entry point (e.g. daily.py Stage 2a). Selects active jobright
    jobs missing ``ats_apply_url`` (skipping prior ``serper_miss`` rows),
    resolves each in a thread pool, and flushes results in batches.

    Returns a counts dict with int values plus two outage signals:
    ``{'resolved','missed','skipped','errors','aborted','infra_error','infra_detail'}``.
    ``aborted`` counts rows left unqueried because the Serper circuit-breaker
    tripped mid-run (a DOWN dependency) — these are NOT written as misses, so they
    stay eligible for retry. ``infra_error`` is 1 if Serper signalled an
    infrastructure failure (credit/auth/quota), else 0; ``infra_detail`` carries
    the cause string (e.g. ``HTTP 400: ... Not enough credits``) for alerting.
    ``errors`` counts rows whose network resolve raised a non-infra error.

    A non-zero ``infra_error`` means the caller (daily.py Stage 2.5) MUST flip the
    run to degraded + alert a human — it is the signal that was missing while a
    dead Serper went unnoticed for days.

    Raises:
        RuntimeError: if ``serper_api_key`` is not configured. (A dict-returning
        function cannot signal this via an exit code; ``main()`` catches it and
        maps it to exit code 2.)
    """
    settings = get_settings()
    key = settings.serper_api_key
    if not key:
        # Never log the key value; only its absence.
        raise RuntimeError("serper_api_key not configured")
    logger.info(f"serper key present (len={len(key)}), dry_run={dry_run}, limit={limit}")

    counts = {"resolved": 0, "missed": 0, "skipped": 0, "errors": 0, "aborted": 0}
    # Serper circuit-breaker: holds the failure-cause string once a SerperInfraError
    # trips it mid-run, else None. Function-scoped so the post-loop return + alert
    # can read it after the worker pool closes.
    abort: dict[str, str | None] = {"infra": None}

    limit_sql = f" LIMIT {int(limit)}" if limit else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT job_id, company_name, role_title
            FROM jobs
            WHERE status='active'
              AND (ats_apply_url IS NULL OR ats_apply_url='')
              AND primary_url ILIKE '%%jobright%%'
              AND company_name IS NOT NULL AND role_title IS NOT NULL
              AND (jd_fetch_source IS NULL
                   OR jd_fetch_source NOT IN ('skip_no_url', 'serper_miss'))
            ORDER BY first_seen_at DESC
            {limit_sql}
            """
        ).fetchall()
        total = len(rows)
        logger.info(f"jobright jobs missing ats_apply_url to resolve: {total}")
        if total == 0:
            print("RESOLVE_DONE resolved=0 missed=0 pct=0 infra_error=0")
            return {**counts, "infra_error": 0, "infra_detail": ""}

        official = aggregator = 0
        updates: list[tuple[str, str]] = []   # (ats_apply_url, job_id)
        misses: list[str] = []
        _RESOLVED_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_fh = None if dry_run else _RESOLVED_LOG.open("a")

        def _resolve_one(r) -> tuple[str, str | None, str]:
            """Pure network step (thread-safe: httpx client per call). No DB.

            Returns (job_id, url_or_None, source). ``source='__aborted__'`` means
            the Serper breaker was already tripped (or tripped on this row) — the
            row was NOT queried and MUST NOT be written as a miss, so it stays
            eligible for retry after the dependency recovers.
            """
            jid = r["job_id"] if isinstance(r, dict) else r[0]
            if abort["infra"]:
                return jid, None, "__aborted__"
            company = (r["company_name"] if isinstance(r, dict) else r[1]) or ""
            title = (r["role_title"] if isinstance(r, dict) else r[2]) or ""
            with httpx.Client(
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            ) as client:
                for q in (f'"{title}" "{company}" careers apply',
                          f'{title} {company} apply careers'):
                    try:
                        organic = _serper(client, key, q)
                    except SerperInfraError as exc:
                        # Trip the breaker (first writer wins) and bail WITHOUT
                        # poisoning this row as a miss.
                        if not abort["infra"]:
                            abort["infra"] = str(exc)
                        return jid, None, "__aborted__"
                    url, src = _best_url(organic, client, verify=verify)
                    if url:
                        return jid, url, src
            return jid, None, "none"

        done = 0
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for jid, url, src in pool.map(_resolve_one, rows):
                done += 1
                if src == "__aborted__":
                    # Breaker tripped: dependency down. Not a miss — leave the row
                    # untouched (no skip_no_url stamp) so it retries next run.
                    counts["aborted"] += 1
                    continue
                if url:
                    counts["resolved"] += 1
                    if src == "serper":
                        official += 1
                    else:
                        aggregator += 1
                    if not dry_run:
                        updates.append((url, jid))
                        log_fh.write(f"{jid}\t{url}\t{src}\n")
                else:
                    counts["missed"] += 1
                    if not dry_run:
                        misses.append(jid)

                # flush batch every 100 (serial DB writes — single connection)
                if not dry_run and (len(updates) >= 100 or len(misses) >= 100):
                    _flush(conn, updates, misses)
                    log_fh.flush()
                    updates, misses = [], []

                if done % 200 == 0:
                    logger.info(
                        f"  {done}/{total} | resolved={counts['resolved']} "
                        f"(official={official}, agg={aggregator}) missed={counts['missed']}"
                    )

        if not dry_run:
            _flush(conn, updates, misses)
            if log_fh:
                log_fh.close()

    infra_detail = abort["infra"] or ""
    infra_error = 1 if infra_detail else 0
    # Denominator excludes aborted rows: pct is "of what we actually queried".
    queried = counts["resolved"] + counts["missed"]
    pct = 100 * counts["resolved"] / max(queried, 1)
    if infra_error:
        logger.error(
            "Serper dependency DOWN — circuit-breaker tripped. Resolved "
            f"{counts['resolved']} before the trip; {counts['aborted']} rows left "
            "UNQUERIED and NOT poisoned (they retry next run). Human action "
            f"required. cause: {infra_detail}"
        )
    logger.info(
        f"DONE: resolved={counts['resolved']}/{total} ({pct:.0f}% of {queried} "
        f"queried) official={official} aggregator={aggregator} "
        f"missed={counts['missed']} aborted={counts['aborted']} infra_error={infra_error}"
    )
    print(
        f"RESOLVE_DONE resolved={counts['resolved']} "
        f"missed={counts['missed']} pct={pct:.0f} infra_error={infra_error}"
    )
    return {**counts, "infra_error": infra_error, "infra_detail": infra_detail}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-verify", action="store_true", help="skip HEAD liveness check (faster)")
    ap.add_argument("--workers", type=int, default=16, help="parallel Serper workers")
    args = ap.parse_args()

    try:
        resolve_jobright_pending(
            limit=args.limit,
            workers=args.workers,
            dry_run=args.dry_run,
            verify=not args.no_verify,
        )
    except RuntimeError as exc:
        logger.error(str(exc))
        return 2
    return 0


def _flush(conn, updates: list[tuple[str, str]], misses: list[str]) -> None:
    """Write a batch: ats_apply_url for resolved (NO jd_fetch_source stamp — the
    resolver finds a URL, it does not fetch a JD), jd_fetch_source='skip_no_url'
    for misses (a constraint-legal "no URL" sentinel; see alembic 0010
    ck_jd_source_requires_usable_jd)."""
    import hashlib
    with conn.cursor() as cur:
        for url, jid in updates:
            # Durable propagation fix: the Firestore sync receiver
            # (shouldUpsertMatchingJob) only re-writes a doc when content_hash
            # changes — and ats_apply_url is NOT part of content_hash
            # (=sha256(company|role)), so a resolved URL alone would be
            # silently dropped at sync. Bump content_hash to include the URL so
            # the receiver detects the change and writes atsApplyUrl through.
            new_ch = hashlib.sha256(f"{jid}|{url}".encode()).hexdigest()
            # Fix #4 (Option B): stamp embedded_at = now() on RESOLVED rows ONLY.
            # INVARIANT: the Stage 4 Firestore sync selects rows by a durable
            # watermark keyed on embedded_at (firebase_active_embedded_at). A
            # jobright row that is already embedded only gets a content_hash
            # change here — its embedded_at would stay in the past, so the
            # watermark (already advanced beyond it) would NEVER re-select it
            # and the freshly-resolved ats_apply_url would never reach Firestore.
            # Bumping embedded_at=now() makes the existing watermark re-select
            # this row on the next sync. Misses (below) are deliberately NOT
            # bumped: nothing about them needs to propagate.
            cur.execute(
                # No jd_fetch_source stamp: resolving an apply URL is NOT a JD
                # fetch, and stamping a non-sentinel source on a thin/empty JD
                # violates ck_jd_source_requires_usable_jd (alembic 0010).
                # embedded_at is bumped ONLY when the row is already embedded
                # (embedding IS NOT NULL); bumping it on an unembedded row would
                # violate ck_embedded_requires_vector — and an unembedded row
                # gets its embedded_at from the embed stage anyway.
                "UPDATE jobs SET ats_apply_url = %(u)s, content_hash = %(ch)s, "
                "embedded_at = CASE WHEN embedding IS NOT NULL THEN now() "
                "ELSE embedded_at END "
                "WHERE job_id = %(j)s AND (ats_apply_url IS NULL OR ats_apply_url='')",
                {"u": url, "ch": new_ch, "j": jid},
            )
        for jid in misses:
            cur.execute(
                # 'skip_no_url' (not 'serper_miss'): a constraint-legal sentinel
                # (alembic 0010 allow-list) meaning "no fetchable ATS URL found",
                # which is exactly a serper miss. 'serper_miss' on a thin JD
                # violated ck_jd_source_requires_usable_jd and crashed Stage 2.5.
                "UPDATE jobs SET jd_fetch_source = 'skip_no_url' "
                "WHERE job_id = %(j)s AND (ats_apply_url IS NULL OR ats_apply_url='')",
                {"j": jid},
            )
    conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())
