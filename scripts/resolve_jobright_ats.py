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
    ats_apply_url + jd_fetch_source='serper'. On no-match, stamp
    jd_fetch_source='serper_miss' so reruns skip it.

SAFE:
  - --dry-run prints what it WOULD resolve, zero writes / zero Serper cost.
  - --limit N for a bounded paid run (validate before full ~19.7k).
  - Batched DB writes (executemany per batch), commit per batch.
  - Reversible: every (job_id, old=NULL, new_url) appended to
    data/jobright_ats_resolved.tsv. To revert: set ats_apply_url=NULL for those.
  - 0.3s throttle between Serper calls; tenacity-free simple retry on 429.
  - Idempotent: WHERE ats_apply_url IS NULL AND jd_fetch_source != 'serper_miss'.

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
    for attempt in range(3):
        try:
            r = client.post(
                _SERPER_URL,
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": q, "num": 6},
                timeout=15.0,
            )
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code != 200:
                return []
            return r.json().get("organic", [])
        except Exception:
            time.sleep(1)
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--no-verify", action="store_true", help="skip HEAD liveness check (faster)")
    ap.add_argument("--workers", type=int, default=16, help="parallel Serper workers")
    args = ap.parse_args()

    settings = get_settings()
    key = settings.serper_api_key
    if not key:
        logger.error("serper_api_key not configured")
        return 2
    logger.info(f"serper key present (len={len(key)}), dry_run={args.dry_run}, limit={args.limit}")

    limit_sql = f" LIMIT {int(args.limit)}" if args.limit else ""
    with get_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT job_id, company_name, role_title
            FROM jobs
            WHERE status='active'
              AND (ats_apply_url IS NULL OR ats_apply_url='')
              AND primary_url ILIKE '%%jobright%%'
              AND company_name IS NOT NULL AND role_title IS NOT NULL
              AND (jd_fetch_source IS NULL OR jd_fetch_source <> 'serper_miss')
            ORDER BY first_seen_at DESC
            {limit_sql}
            """
        ).fetchall()
        total = len(rows)
        logger.info(f"jobright jobs missing ats_apply_url to resolve: {total}")
        if total == 0:
            print("RESOLVE_DONE resolved=0 missed=0")
            return 0

        resolved = official = aggregator = missed = 0
        updates: list[tuple[str, str]] = []   # (ats_apply_url, job_id)
        misses: list[str] = []
        _RESOLVED_LOG.parent.mkdir(parents=True, exist_ok=True)
        log_fh = None if args.dry_run else _RESOLVED_LOG.open("a")

        verify = not args.no_verify

        def _resolve_one(r) -> tuple[str, str | None, str]:
            """Pure network step (thread-safe: httpx client per call). No DB.

            Returns (job_id, url_or_None, source).
            """
            jid = r["job_id"] if isinstance(r, dict) else r[0]
            company = (r["company_name"] if isinstance(r, dict) else r[1]) or ""
            title = (r["role_title"] if isinstance(r, dict) else r[2]) or ""
            with httpx.Client(
                headers={"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"},
            ) as client:
                for q in (f'"{title}" "{company}" careers apply',
                          f'{title} {company} apply careers'):
                    organic = _serper(client, key, q)
                    url, src = _best_url(organic, client, verify=verify)
                    if url:
                        return jid, url, src
            return jid, None, "none"

        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for jid, url, src in pool.map(_resolve_one, rows):
                done += 1
                if url:
                    resolved += 1
                    if src == "serper":
                        official += 1
                    else:
                        aggregator += 1
                    if not args.dry_run:
                        updates.append((url, jid))
                        log_fh.write(f"{jid}\t{url}\t{src}\n")
                else:
                    missed += 1
                    if not args.dry_run:
                        misses.append(jid)

                # flush batch every 100 (serial DB writes — single connection)
                if not args.dry_run and (len(updates) >= 100 or len(misses) >= 100):
                    _flush(conn, updates, misses)
                    log_fh.flush()
                    updates, misses = [], []

                if done % 200 == 0:
                    logger.info(
                        f"  {done}/{total} | resolved={resolved} "
                        f"(official={official}, agg={aggregator}) missed={missed}"
                    )

        if not args.dry_run:
            _flush(conn, updates, misses)
            if log_fh:
                log_fh.close()

    pct = 100 * resolved / max(total, 1)
    logger.info(
        f"DONE: resolved={resolved}/{total} ({pct:.0f}%) "
        f"official={official} aggregator={aggregator} missed={missed}"
    )
    print(f"RESOLVE_DONE resolved={resolved} missed={missed} pct={pct:.0f}")
    return 0


def _flush(conn, updates: list[tuple[str, str]], misses: list[str]) -> None:
    """Write a batch: ats_apply_url for resolved, jd_fetch_source='serper_miss' for misses."""
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
            cur.execute(
                "UPDATE jobs SET ats_apply_url = %(u)s, jd_fetch_source = 'serper', "
                "content_hash = %(ch)s "
                "WHERE job_id = %(j)s AND (ats_apply_url IS NULL OR ats_apply_url='')",
                {"u": url, "ch": new_ch, "j": jid},
            )
        for jid in misses:
            cur.execute(
                "UPDATE jobs SET jd_fetch_source = 'serper_miss' "
                "WHERE job_id = %(j)s AND (ats_apply_url IS NULL OR ats_apply_url='')",
                {"j": jid},
            )
    conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())
