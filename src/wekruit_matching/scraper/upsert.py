"""Upsert pipeline for scraped job listings.

Writes Job objects to the jobs table using ON CONFLICT (job_id) DO UPDATE.
The idempotent upsert pattern ensures re-running the scraper on unchanged data
produces zero DB writes beyond last_seen_at bookkeeping.

Stale marking (mark_stale_jobs) sets status='inactive' for jobs that
disappeared from the README — it never deletes rows, preserving history
for Phase 3 enrichment context.

P7-K (2026-05-09) — Postgres dead tombstone
-------------------------------------------
After 0007 added ``dead`` / ``dead_confirmed_at`` columns, this module is
the gate that prevents the dead-URL infinite-loop scenario described in
that migration. On every batch:

1. Pre-pass: SELECT existing dead state for the batch's job_ids
2. Recovery pass: any row with ``dead=true AND dead_confirmed_at < NOW()
   - INTERVAL '90 days'`` is reset (dead=false, dead_confirmed_at=NULL)
   to allow ONE retry. Capped at 100 rows per pipeline run so a flood of
   stale tombstones can't undo all of them in one pass.
3. Skip pass: any row still ``dead=true`` (within 30/90d window OR with
   NULL dead_confirmed_at as legacy backfill) is *removed from the input
   set*. The normal UPSERT below then sees fewer rows and never resets
   their status from 'inactive' → 'active'.

Logs ``pa.scraper.skipped_dead_jobs {count: N}`` on every run for
ops dashboards.
"""
import os
from collections.abc import Collection
from datetime import UTC, datetime

import psycopg
from loguru import logger

from wekruit_matching.enrichment.readiness import is_matchable_ready
from wekruit_matching.models.job import Job


def _utcnow() -> datetime:
    return datetime.now(UTC)


_UPSERT_BATCH_SIZE = 500

# P7-K constants
_DEAD_RETRY_AGE_DAYS = 90        # tombstone older than this => allow one retry
_DEAD_RETRY_MAX_PER_RUN = 100    # safety cap so we don't undo all tombstones at once


def _norm(s: str | None) -> str:
    """Lowercase + collapse whitespace for stable-identity matching.

    Mirrors the ``lower(btrim(...))`` normalization used in the SQL carry-forward
    / integrity queries so the Python-side identity key agrees with Postgres.
    """
    return " ".join((s or "").strip().lower().split())


def _carry_forward_first_seen(
    batch: list[Job],
    conn: psycopg.Connection,
) -> dict[str, datetime]:
    """Return {job_id -> earliest first_seen_at for that job's stable identity}.

    first_seen_at preservation (recency-signal reliability)
    -------------------------------------------------------
    ``first_seen_at`` is the recency signal the downstream matcher relies on.
    ``job_id`` is a content hash of (source_repo, norm company, norm role) — see
    ``id_utils.generate_job_id``. Whenever those inputs change (the v1->v2 hash
    migration, or a source simply editing a company/title string), the job
    re-hashes to a BRAND NEW job_id. ``ON CONFLICT (job_id)`` then never fires,
    so the INSERT below stamps ``first_seen_at = now()`` and the original row is
    orphaned (later stale-marked). Observed on live data: the entire active
    corpus had ``first_seen_at`` within ~3 days; 2,030 active rows had a
    first_seen_at newer than an older sibling for the same identity.

    Fix: look up the earliest ``first_seen_at`` ever recorded for each job's
    stable identity ``(norm company, norm role, source_repo)`` across ALL rows
    (any status) and carry it forward. Genuinely-new identities are absent from
    the result and fall back to ``now()`` in the caller. One batched query per
    upsert batch — no per-row round-trips.
    """
    if not batch:
        return {}

    companies = [j.company_name for j in batch]
    roles = [j.role_title for j in batch]
    repos = [j.source_repo for j in batch]
    rows = conn.execute(
        """
        SELECT lower(btrim(company_name)) AS c,
               lower(btrim(role_title))   AS r,
               source_repo                AS s,
               min(first_seen_at)         AS min_seen
        FROM jobs
        WHERE (lower(btrim(company_name)), lower(btrim(role_title)), source_repo)
              IN (
                SELECT lower(btrim(c)), lower(btrim(r)), s
                FROM unnest(%(companies)s::text[], %(roles)s::text[], %(repos)s::text[])
                     AS t(c, r, s)
              )
        GROUP BY 1, 2, 3
        """,
        {"companies": companies, "roles": roles, "repos": repos},
    ).fetchall()

    by_ident: dict[tuple[str, str, str | None], datetime] = {
        (row["c"], row["r"], row["s"]): row["min_seen"] for row in rows
    }
    out: dict[str, datetime] = {}
    for j in batch:
        key = (_norm(j.company_name), _norm(j.role_title), j.source_repo)
        prior = by_ident.get(key)
        if prior is not None:
            out[j.job_id] = prior
    return out


def _filter_dead_tombstoned(
    jobs: list[Job],
    conn: psycopg.Connection,
) -> tuple[list[Job], int, int]:
    """Strip dead-tombstoned jobs from ``jobs`` and recover any past 90d.

    Returns: (filtered_jobs, skipped_count, retried_count)

    Behaviour (matches P9 directive):
      * dead=true AND dead_confirmed_at < NOW() - 90d : retry path
        - reset dead=false, dead_confirmed_at=NULL (capped at 100/run)
        - keep job in the filtered list (normal UPSERT proceeds)
      * dead=true AND dead_confirmed_at >= NOW() - 90d : skip
        (covers the 30-day skip-window + the 30-90d hold)
      * dead=true AND dead_confirmed_at IS NULL (legacy backfill) : skip
        — we don't know how old the tombstone is, treat as recent
      * dead=false / NULL : pass through unchanged

    Always uses a single SELECT + (optional) UPDATE; no per-row queries.
    Empty / never-seen-before batches return immediately.
    """
    if not jobs:
        return jobs, 0, 0

    job_ids = [j.job_id for j in jobs]
    rows = conn.execute(
        """
        SELECT job_id, dead, dead_confirmed_at
        FROM jobs
        WHERE job_id = ANY(%(ids)s)
          AND dead IS TRUE
        """,
        {"ids": job_ids},
    ).fetchall()
    if not rows:
        return jobs, 0, 0

    # Partition: which dead rows are eligible for the 90d retry path?
    retry_ids: list[str] = []
    skip_ids: set[str] = set()
    cutoff = _utcnow().replace(tzinfo=UTC)
    for r in rows:
        confirmed_at = r["dead_confirmed_at"]
        if confirmed_at is None:
            # Legacy / Stage-0 backfill with no timestamp. Treat as recent
            # to be safe — single 90d retry can fire next time once the
            # liveness sweep re-confirms.
            skip_ids.add(r["job_id"])
            continue
        # Normalize tz-naive timestamps from psycopg to UTC for comparison
        if confirmed_at.tzinfo is None:
            confirmed_at = confirmed_at.replace(tzinfo=UTC)
        age_days = (cutoff - confirmed_at).total_seconds() / 86400
        if age_days >= _DEAD_RETRY_AGE_DAYS:
            retry_ids.append(r["job_id"])
        else:
            skip_ids.add(r["job_id"])

    # Cap retries per run (safety: 100 stale tombstones don't all reset together)
    if len(retry_ids) > _DEAD_RETRY_MAX_PER_RUN:
        # Excess retries get demoted back into the skip set this run; they'll
        # be eligible again next pipeline run.
        skip_ids.update(retry_ids[_DEAD_RETRY_MAX_PER_RUN:])
        retry_ids = retry_ids[:_DEAD_RETRY_MAX_PER_RUN]

    # Reset the retry-eligible rows so the subsequent UPSERT is allowed to
    # re-activate them. dead=false + dead_confirmed_at=NULL means "we'll
    # let the next liveness sweep tell us if this URL is really dead".
    if retry_ids:
        conn.execute(
            """
            UPDATE jobs
            SET dead = FALSE,
                dead_confirmed_at = NULL
            WHERE job_id = ANY(%(ids)s)
            """,
            {"ids": retry_ids},
        )
        conn.commit()

    if skip_ids:
        filtered = [j for j in jobs if j.job_id not in skip_ids]
        logger.info(
            "pa.scraper.skipped_dead_jobs count={} retried={}",
            len(skip_ids),
            len(retry_ids),
        )
    else:
        filtered = jobs
        if retry_ids:
            logger.info(
                "pa.scraper.skipped_dead_jobs count=0 retried={}",
                len(retry_ids),
            )

    return filtered, len(skip_ids), len(retry_ids)


def upsert_jobs(jobs: list[Job], conn: psycopg.Connection) -> dict[str, int]:
    """Batch upsert Job records into the jobs table.

    Uses UNNEST-based batch INSERT ... ON CONFLICT for 50-100x speedup
    over row-by-row. Processes in chunks of 500 for Supabase timeout safety.

    P10-audit fix (2026-05-06): persist seniority_level, role_function,
    sources, and job_description on insert (previously dropped silently —
    scrapers set them but they never reached the DB).

    P7-K (2026-05-09): pre-filter dead-tombstoned jobs (defense-in-depth
    against the dead-URL infinite-loop). See ``_filter_dead_tombstoned``.

    Returns: {"inserted": N, "updated": N, "unchanged": N,
              "skipped_dead": N, "dead_retried": N}
    """
    if not jobs:
        return {
            "inserted": 0, "updated": 0, "unchanged": 0,
            "skipped_dead": 0, "dead_retried": 0,
        }

    # P7-K — strip dead-tombstoned URLs before any UPSERT touches them.
    # Done once for the whole call rather than per-batch because the
    # tombstone set is small (typically <1% of inputs) and one SELECT
    # covering all job_ids is cheaper than N batched ones.
    jobs, skipped_dead, dead_retried = _filter_dead_tombstoned(jobs, conn)

    inserted = updated = unchanged = 0

    if not jobs:
        # Whole input was tombstoned. Skip the UPSERT loop entirely.
        logger.info(
            "Upserted 0 jobs: 0 inserted, 0 updated, 0 unchanged "
            "(skipped_dead={} dead_retried={})",
            skipped_dead, dead_retried,
        )
        return {
            "inserted": 0, "updated": 0, "unchanged": 0,
            "skipped_dead": skipped_dead, "dead_retried": dead_retried,
        }

    for i in range(0, len(jobs), _UPSERT_BATCH_SIZE):
        batch = jobs[i : i + _UPSERT_BATCH_SIZE]
        now = _utcnow()

        # Collect existing hashes for this batch to detect changes
        batch_ids = [j.job_id for j in batch]
        existing = {}
        if batch_ids:
            rows = conn.execute(
                "SELECT job_id, content_hash FROM jobs WHERE job_id = ANY(%(ids)s)",
                {"ids": batch_ids},
            ).fetchall()
            existing = {r["job_id"]: r["content_hash"] for r in rows}

        # first_seen_at preservation: carry forward the earliest first_seen_at
        # recorded for each job's stable identity so a job_id re-hash never
        # resets the recency signal. Genuinely-new jobs fall back to ``now``.
        first_seen_map = _carry_forward_first_seen(batch, conn)

        # Batch upsert using cursor.executemany (psycopg3)
        conn.cursor().executemany(
            """
            INSERT INTO jobs (
                job_id, source_repo, company_name, role_title,
                primary_url, ats_apply_url, location_raw, date_posted_raw,
                status, first_seen_at, last_seen_at, content_hash,
                industry, company_size, required_skills, sponsorship,
                enriched_at,
                seniority_level, role_function, sources,
                job_description
            ) VALUES (
                %(job_id)s, %(source_repo)s, %(company_name)s, %(role_title)s,
                %(primary_url)s, %(ats_apply_url)s, %(location_raw)s, %(date_posted_raw)s,
                'active', %(first_seen_at)s, %(now)s, %(content_hash)s,
                %(industry)s, %(company_size)s, %(required_skills)s, %(sponsorship)s,
                %(enriched_at)s,
                %(seniority_level)s, %(role_function)s, %(sources)s,
                %(job_description)s
            )
            ON CONFLICT (job_id) DO UPDATE SET
                location_raw    = EXCLUDED.location_raw,
                date_posted_raw = EXCLUDED.date_posted_raw,
                last_seen_at    = EXCLUDED.last_seen_at,
                -- Matching-quality launch blocker (2026-05-20): preserve
                -- ``status`` when a PA hygiene flip has set hygiene_flipped=TRUE.
                -- Without this, every scrape rerun resets hygiene-flipped docs
                -- (yc_synthetic_title / jd_zombie / apply_url_not_job_page) back
                -- to 'active' and the next sync writes them to Firestore — the
                -- hygiene work is undone within 24h. See alembic 0008.
                status          = CASE
                    WHEN COALESCE(jobs.hygiene_flipped, FALSE) IS TRUE
                    THEN jobs.status
                    ELSE 'active'
                END,
                content_hash    = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN EXCLUDED.content_hash
                    ELSE jobs.content_hash
                END,
                enriched_at     = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN NULL
                    ELSE jobs.enriched_at
                END,
                embedding       = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN NULL
                    ELSE jobs.embedding
                END,
                embedding_model = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN NULL
                    ELSE jobs.embedding_model
                END,
                embedded_at     = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN NULL
                    ELSE jobs.embedded_at
                END,
                -- rank-16 fix: a re-listed posting whose content_hash CHANGED is
                -- genuinely back. Clear the permanent_404 tombstone + the
                -- terminal jd_fetch_source sentinel so the Stage 2b JD queue
                -- re-admits it; otherwise the row flips active but
                -- permanent_404=TRUE keeps it permanently out of the JD queue
                -- and reconcile_dead_inactive re-flips it inactive every run
                -- (active<->inactive thrash, never matchable).
                permanent_404   = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                    THEN FALSE
                    ELSE jobs.permanent_404
                END,
                jd_fetch_source = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                         AND jobs.jd_fetch_source IN ('closed_at_source', 'failed', 'skip_no_url')
                    THEN NULL
                    ELSE jobs.jd_fetch_source
                END,
                jd_fetch_attempted_at = CASE
                    WHEN jobs.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                         AND jobs.jd_fetch_source IN ('closed_at_source', 'failed', 'skip_no_url')
                    THEN NULL
                    ELSE jobs.jd_fetch_attempted_at
                END,
                -- P10-audit fix: keep seniority_level / role_function /
                -- sources / job_description fresh on every upsert. These
                -- are derived from role_title which is stable per job_id,
                -- so overwriting with EXCLUDED is always safe.
                seniority_level = COALESCE(EXCLUDED.seniority_level, jobs.seniority_level),
                role_function   = CASE
                    WHEN cardinality(EXCLUDED.role_function) > 0
                    THEN EXCLUDED.role_function
                    ELSE jobs.role_function
                END,
                sources         = CASE
                    WHEN cardinality(EXCLUDED.sources) > 0
                    THEN (
                        SELECT array_agg(DISTINCT s)
                        FROM unnest(jobs.sources || EXCLUDED.sources) AS s
                    )
                    ELSE jobs.sources
                END,
                job_description = COALESCE(EXCLUDED.job_description, jobs.job_description),
                -- 2026-05-18 v1.8 — scraper-emitted ats_apply_url (non-null
                -- when primary_url is a real ATS URL, not jobright). Carry
                -- forward existing non-null over re-scrapes; only fill when
                -- previously null.
                ats_apply_url   = COALESCE(jobs.ats_apply_url, EXCLUDED.ats_apply_url)
            """,
            [
                {
                    "job_id": job.job_id,
                    "source_repo": job.source_repo,
                    "company_name": job.company_name,
                    "role_title": job.role_title,
                    "primary_url": job.primary_url,
                    "ats_apply_url": job.ats_apply_url,
                    "location_raw": job.location_raw,
                    "date_posted_raw": job.date_posted_raw,
                    "content_hash": job.content_hash,
                    "industry": job.industry,
                    "company_size": job.company_size,
                    "required_skills": job.required_skills or [],
                    "sponsorship": job.sponsorship,
                    # rank-14 fix: stamp enriched_at on the SHARED readiness
                    # predicate (usable JD + skills), NOT on industry presence.
                    # jobright_github always sets industry=category with empty
                    # skills, so the old `if job.industry` stamped enriched_at on
                    # a 0-skill row -> excluded from the gap-fill re-enricher for
                    # 7 days and from the embed gate forever (the lockout class).
                    "enriched_at": (
                        now
                        if is_matchable_ready(job.job_description, job.required_skills)
                        else None
                    ),
                    "seniority_level": job.seniority_level,
                    "role_function": job.role_function or [],
                    "sources": job.sources or [],
                    "job_description": job.job_description,
                    "now": now,
                    # Carry-forward earliest first_seen_at for this identity, or
                    # ``now`` for a genuinely-new job. ON CONFLICT does NOT touch
                    # first_seen_at, so existing rows keep their stored value.
                    "first_seen_at": first_seen_map.get(job.job_id, now),
                }
                for job in batch
            ],
        )
        conn.commit()

        # Count results from pre-fetched hashes
        for job in batch:
            if job.job_id not in existing:
                inserted += 1
            elif existing[job.job_id] != job.content_hash:
                updated += 1
            else:
                unchanged += 1

    logger.info(
        "Upserted {} jobs: {} inserted, {} updated, {} unchanged "
        "(skipped_dead={} dead_retried={})",
        len(jobs), inserted, updated, unchanged,
        skipped_dead, dead_retried,
    )

    # Carry forward ats_apply_url from recently-deactivated jobs to new active
    # rows with the same company+title. Prevents re-burning Serper credits on
    # jobs that just got a new job_id from the source repo.
    if inserted > 0:
        recovered = conn.execute(
            """
            UPDATE jobs a
            SET ats_apply_url = b.ats_apply_url,
                jd_fetch_source = b.jd_fetch_source
            FROM (
                SELECT DISTINCT ON (company_name, role_title)
                       company_name, role_title, ats_apply_url, jd_fetch_source
                FROM jobs
                WHERE status = 'inactive'
                  AND ats_apply_url IS NOT NULL
                  AND last_seen_at > NOW() - INTERVAL '30 days'
                ORDER BY company_name, role_title, last_seen_at DESC
            ) b
            WHERE a.status = 'active'
              AND a.ats_apply_url IS NULL
              AND a.company_name = b.company_name
              AND a.role_title = b.role_title
            """,
        ).rowcount
        conn.commit()
        if recovered > 0:
            logger.info("Carried forward {} ats_apply_url from inactive jobs", recovered)

    return {
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "skipped_dead": skipped_dead,
        "dead_retried": dead_retried,
    }


_STALE_BATCH_SIZE = 5000

# Circuit-breaker (reliability audit 2026-06-01, ranks 9-13). A partial/failed
# scrape returns a truncated seen-set; mark_stale_jobs would then flip every
# active row NOT in that set to inactive, silently mass-deactivating live jobs
# (observed across jobright_github / direct-ATS / social / vc_board / simplify).
# If a single run would deactivate more than this FRACTION of a repo's current
# active rows, treat it as a partial fetch and REFUSE — unless force=True.
# Tunable via env for the rare legitimate bulk-clear.
_STALE_MAX_DEACTIVATION_FRACTION = float(
    os.environ.get("STALE_MAX_DEACTIVATION_FRACTION", "0.5")
)
# Below this many active rows the fraction guard is noisy/irrelevant (a small
# board legitimately churning a few jobs), so the guard only engages at/above it.
_STALE_GUARD_MIN_ACTIVE = int(os.environ.get("STALE_GUARD_MIN_ACTIVE", "20"))

# Sentinel return: the circuit-breaker tripped and NOTHING was deactivated.
# Negative so callers can distinguish "guard blocked" from "0 were stale".
STALE_GUARD_TRIPPED = -1


def _count_active(conn: psycopg.Connection, source_repo: str) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE source_repo = %(r)s AND status = 'active'",
        {"r": source_repo},
    ).fetchone()
    return int(row["n"]) if row else 0


def mark_stale_jobs(
    seen_ids: Collection[str],
    source_repo: str,
    conn: psycopg.Connection,
    *,
    force: bool = False,
) -> int:
    """Mark active jobs from source_repo as inactive if their job_id is not in seen_ids.

    Called after upsert to deactivate listings that disappeared from the README.
    Never deletes rows — preserves history for enrichment context.
    Scoped to source_repo — stale marking for one repo never affects another.

    For large ID sets (>5000), uses a two-step approach to avoid statement
    timeouts on Supabase's pooler: first collects active IDs, then batches
    updates on the smaller stale subset.

    Circuit-breaker (2026-06-01): refuses to deactivate when the seen-set is
    implausibly small vs the repo's current active count — the signature of a
    partial/failed scrape — to prevent silent mass-deactivation of live jobs.
    Pass ``force=True`` for a legitimate bulk-clear (e.g. a repo truly emptied).
    When the guard trips it deactivates NOTHING and returns
    ``STALE_GUARD_TRIPPED`` (-1) so the caller can surface a dependency error.

    Returns: count of rows marked inactive, or ``STALE_GUARD_TRIPPED`` if the
    circuit-breaker blocked a suspicious mass-deactivation.
    """
    seen_set = set(seen_ids)

    # Circuit-breaker: how many active rows would this run deactivate?
    if not force:
        active_n = _count_active(conn, source_repo)
        if active_n >= _STALE_GUARD_MIN_ACTIVE:
            would_deactivate = conn.execute(
                """
                SELECT count(*) AS n FROM jobs
                WHERE source_repo = %(r)s AND status = 'active'
                  AND NOT (job_id = ANY(%(seen)s))
                """,
                {"r": source_repo, "seen": list(seen_set)},
            ).fetchone()
            would_n = int(would_deactivate["n"]) if would_deactivate else 0
            frac = would_n / active_n if active_n else 0.0
            if frac > _STALE_MAX_DEACTIVATION_FRACTION:
                logger.error(
                    "STALE GUARD TRIPPED for repo {}: run would deactivate {}/{} "
                    "active rows ({:.0%} > {:.0%} limit) from a seen-set of {} — "
                    "treating as a PARTIAL/failed scrape and deactivating NOTHING. "
                    "Pass force=True for a legitimate bulk clear.",
                    source_repo, would_n, active_n, frac,
                    _STALE_MAX_DEACTIVATION_FRACTION, len(seen_set),
                )
                return STALE_GUARD_TRIPPED

    if not seen_ids:
        # Edge case: all jobs disappeared. Reaching here means either force=True
        # or the repo had < _STALE_GUARD_MIN_ACTIVE active rows (guard skipped),
        # so a full clear is acceptable.
        result = conn.execute(
            """
            UPDATE jobs
            SET status = 'inactive'
            WHERE source_repo = %(source_repo)s AND status = 'active'
            """,
            {"source_repo": source_repo},
        )
        conn.commit()
        count = result.rowcount
        logger.info("Marked {} stale jobs inactive for repo {}", count, source_repo)
        return count

    total_marked = 0

    if len(seen_set) <= _STALE_BATCH_SIZE:
        # Small set — single NOT IN query is fast enough
        result = conn.execute(
            """
            UPDATE jobs
            SET status = 'inactive'
            WHERE source_repo = %(source_repo)s
              AND status = 'active'
              AND NOT (job_id = ANY(%(seen_ids)s))
            """,
            {"source_repo": source_repo, "seen_ids": list(seen_set)},
        )
        total_marked = result.rowcount
        conn.commit()
    else:
        # Large set — collect active IDs first, then batch-update stale ones
        logger.info(
            "Large ID set ({}) for {} — using batched stale marking",
            len(seen_set), source_repo,
        )
        active_rows = conn.execute(
            """
            SELECT job_id FROM jobs
            WHERE source_repo = %(source_repo)s AND status = 'active'
            """,
            {"source_repo": source_repo},
        ).fetchall()

        stale_ids = [r["job_id"] for r in active_rows if r["job_id"] not in seen_set]
        logger.info("Found {} stale jobs to deactivate", len(stale_ids))

        for i in range(0, len(stale_ids), _STALE_BATCH_SIZE):
            batch = stale_ids[i : i + _STALE_BATCH_SIZE]
            result = conn.execute(
                """
                UPDATE jobs
                SET status = 'inactive'
                WHERE job_id = ANY(%(stale_ids)s)
                """,
                {"stale_ids": batch},
            )
            total_marked += result.rowcount
            conn.commit()

    logger.info("Marked {} stale jobs inactive for repo {}", total_marked, source_repo)
    return total_marked


def mark_specific_ids_inactive(
    stale_ids: set[str],
    source_repo: str,
    conn: psycopg.Connection,
) -> int:
    """Mark a specific set of job_ids as inactive within source_repo.

    Inverse semantics of ``mark_stale_jobs``:
      * ``mark_stale_jobs(seen_ids, ...)``  marks everything NOT in seen_ids inactive.
      * ``mark_specific_ids_inactive(stale_ids, ...)`` marks EXACTLY stale_ids inactive.

    Used by the pure-diff jobright path (``JOBRIGHT_USE_GIT_DELTA=1``): the
    ``-`` rows in HEAD~1..HEAD give us the canonical removal set; we don't
    need to scan the full README to deduce it.

    Returns
    -------
    Count of rows actually flipped from active -> inactive (no-op if already inactive).
    """
    if not stale_ids:
        return 0
    result = conn.execute(
        """
        UPDATE jobs
        SET status = 'inactive', last_seen_at = %s
        WHERE source_repo = %s
          AND job_id = ANY(%s)
          AND status = 'active'
        """,
        (_utcnow(), source_repo, list(stale_ids)),
    )
    return result.rowcount


# Runtime gate threshold: how many "reset" offenders to tolerate before the
# scrape run logs a regression warning. 0 = strict.
FIRST_SEEN_INTEGRITY_THRESHOLD = 0


def check_first_seen_integrity(conn: psycopg.Connection) -> int:
    """Runtime GATE: count active jobs whose ``first_seen_at`` is newer than an
    older sibling row (any status) sharing the same stable identity
    ``(norm company, norm role, source_repo)``.

    A non-zero result is the signature of a ``first_seen_at`` reset: a job_id
    re-hash inserted a fresh row with ``first_seen_at = now()`` while an older
    row for the same logical job still carries the true (earlier) timestamp.
    The daily scrape calls this after upsert so the failure mode is auto-caught
    on the next run instead of silently degrading match recency. Read-only.
    """
    row = conn.execute(
        """
        WITH ident AS (
            SELECT lower(btrim(company_name)) AS c,
                   lower(btrim(role_title))   AS r,
                   source_repo                AS s,
                   min(first_seen_at)         AS min_seen
            FROM jobs
            GROUP BY 1, 2, 3
        )
        SELECT count(*) AS n
        FROM jobs j
        JOIN ident i
          ON lower(btrim(j.company_name)) = i.c
         AND lower(btrim(j.role_title))   = i.r
         AND j.source_repo IS NOT DISTINCT FROM i.s
        WHERE j.status = 'active'
          AND j.first_seen_at > i.min_seen
        """
    ).fetchone()
    offenders = int(row["n"]) if row and row["n"] is not None else 0
    if offenders > FIRST_SEEN_INTEGRITY_THRESHOLD:
        logger.warning(
            "pa.scraper.first_seen_integrity offenders={} — active rows with a "
            "first_seen_at newer than an older sibling for the same identity "
            "(likely a job_id re-hash reset). Run backfill_first_seen().",
            offenders,
        )
    else:
        logger.info("pa.scraper.first_seen_integrity offenders=0 (clean)")
    return offenders


def backfill_first_seen(conn: psycopg.Connection) -> int:
    """Repair rows whose ``first_seen_at`` was reset by a past job_id re-hash.

    For each stable identity, set every row's ``first_seen_at`` to the earliest
    value recorded across the identity's rows. Idempotent: a second run updates
    0 rows once the table is consistent. This is the one-time remediation for
    the historical reset; the carry-forward in ``upsert_jobs`` prevents future
    occurrences. Caller owns the transaction (no commit here) so it can scope /
    review the write. Returns the number of rows updated.
    """
    result = conn.execute(
        """
        WITH ident AS (
            SELECT lower(btrim(company_name)) AS c,
                   lower(btrim(role_title))   AS r,
                   source_repo                AS s,
                   min(first_seen_at)         AS min_seen
            FROM jobs
            GROUP BY 1, 2, 3
        )
        UPDATE jobs j
           SET first_seen_at = i.min_seen
          FROM ident i
         WHERE lower(btrim(j.company_name)) = i.c
           AND lower(btrim(j.role_title))   = i.r
           AND j.source_repo IS NOT DISTINCT FROM i.s
           AND j.first_seen_at > i.min_seen
        """
    )
    return result.rowcount
