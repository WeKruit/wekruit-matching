"""Hard filter layer for the matching engine.

Filters a list of job dicts by job type, sponsorship requirement, and location
preference. Applied post-ANN retrieval — not as SQL pre-filters — to avoid
shrinking the ANN candidate set and triggering sequential scans on the HNSW index.

Public API:
    apply_hard_filters(jobs, profile) -> list[dict]
    normalize_location(loc) -> str
    LOCATION_ALIASES: dict[str, str]
"""
from __future__ import annotations

from loguru import logger

from wekruit_matching.models.user_profile import JobType, UserProfile

# ---------------------------------------------------------------------------
# Location alias map: raw string (lowercased) -> canonical bucket
# ---------------------------------------------------------------------------

LOCATION_ALIASES: dict[str, str] = {
    # San Francisco
    "sf": "san francisco",
    "san francisco": "san francisco",
    "san francisco, ca": "san francisco",
    "sf, ca": "san francisco",
    # New York
    "nyc": "new york",
    "new york": "new york",
    "new york, ny": "new york",
    "ny": "new york",
    # Los Angeles
    "la": "los angeles",
    "los angeles": "los angeles",
    "los angeles, ca": "los angeles",
    # Seattle
    "seattle": "seattle",
    "seattle, wa": "seattle",
    # Austin
    "austin": "austin",
    "austin, tx": "austin",
    # Boston
    "boston": "boston",
    "boston, ma": "boston",
    # Chicago
    "chicago": "chicago",
    "chicago, il": "chicago",
    # Remote
    "remote": "remote",
}

# Source-repo to JobType mapping (multiple repos per type)
_SOURCE_REPO_SET: dict[JobType, set[str]] = {
    JobType.INTERN: {"Summer2026-Internships", "jobright-intern"},
    JobType.NEW_GRAD: {"New-Grad-Positions", "jobright-newgrad"},
}


# ---------------------------------------------------------------------------
# Location normalization
# ---------------------------------------------------------------------------

def normalize_location(loc: str) -> str:
    """Normalize a raw location string to a canonical bucket.

    Lowercases and strips whitespace, then looks up in LOCATION_ALIASES.
    Returns the canonical bucket if found, otherwise returns the
    lowercased-and-stripped input unchanged.

    Examples:
        normalize_location("SF") -> "san francisco"
        normalize_location("NYC") -> "new york"
        normalize_location("LA") -> "los angeles"
        normalize_location("Remote") -> "remote"
        normalize_location("Unknown City, TX") -> "unknown city, tx"
    """
    key = loc.lower().strip()
    return LOCATION_ALIASES.get(key, key)


def _job_location_buckets(location_raw: str) -> set[str]:
    """Return the set of canonical location buckets for a raw location string.

    Splits on ";" and "," and normalizes each token. If any token normalizes
    to "remote", the result is {"remote"} only (remote is universal).
    """
    if not location_raw:
        return set()

    # Split on semicolon first, then comma
    tokens: list[str] = []
    for part in location_raw.split(";"):
        for sub in part.split(","):
            stripped = sub.strip()
            if stripped:
                tokens.append(stripped)

    buckets: set[str] = set()
    for token in tokens:
        bucket = normalize_location(token)
        if bucket == "remote":
            # Remote overrides everything — remote jobs are universally matching
            return {"remote"}
        buckets.add(bucket)

    return buckets


def _preferred_buckets(preferred_locations: list[str]) -> set[str]:
    """Normalize a list of user-preferred location strings to canonical buckets."""
    return {normalize_location(loc) for loc in preferred_locations}


# ---------------------------------------------------------------------------
# Individual filters
# ---------------------------------------------------------------------------

def filter_by_job_type(jobs: list[dict], job_type: JobType) -> list[dict]:
    """Filter jobs by job type.

    JobType.ANY passes all rows unchanged.
    JobType.INTERN keeps only rows where source_repo == "Summer2026-Internships".
    JobType.NEW_GRAD keeps only rows where source_repo == "New-Grad-Positions".
    """
    if job_type is JobType.ANY:
        return jobs
    allowed_repos = _SOURCE_REPO_SET[job_type]
    return [job for job in jobs if job.get("source_repo") in allowed_repos]


def filter_by_sponsorship(jobs: list[dict], requires_sponsorship: bool) -> list[dict]:
    """Filter jobs by sponsorship requirement, treating UNKNOWN as eligible.

    If requires_sponsorship is False, all rows pass unchanged.

    If requires_sponsorship is True, drop ONLY rows that are *explicitly* known
    to NOT sponsor (sponsorship is False). Rows where sponsorship is True or
    None (unknown) are KEPT.

    Rationale (reliability fix): sponsorship is enriched, not authoritative, and
    on the live corpus the vast majority of jobs have sponsorship = NULL
    (unknown). Treating NULL as "no sponsorship" silently removes ~80%+ of the
    candidate pool for international-student users — a confirmed-good job that
    simply hasn't been classified would never be shown. Unknown must mean
    "maybe", not "no". A False-positive (showing a maybe-no job) is recoverable
    by the user; a False-negative (hiding a real match) is invisible and is the
    worst failure mode for a matching engine.
    """
    if not requires_sponsorship:
        return jobs
    return [job for job in jobs if job.get("sponsorship") is not False]


def filter_by_location(jobs: list[dict], preferred_locations: list[str]) -> list[dict]:
    """Filter jobs by location preference.

    If preferred_locations is empty, all rows pass unchanged (no location filter).
    If "remote" is in the preferred buckets, all jobs pass (user is open to any remote).
    For each job:
      - If the job location normalizes to "remote", it passes any non-empty preference.
      - If the job's location buckets intersect with preferred buckets, it passes.
      - Otherwise it is excluded.
    """
    if not preferred_locations:
        return jobs

    pref_buckets = _preferred_buckets(preferred_locations)

    # User prefers remote -> all jobs pass
    if "remote" in pref_buckets:
        return jobs

    result: list[dict] = []
    for job in jobs:
        job_buckets = _job_location_buckets(job.get("location_raw") or "")
        # Remote job matches any preference
        if "remote" in job_buckets:
            result.append(job)
            continue
        # Non-empty intersection means location matches
        if job_buckets & pref_buckets:
            result.append(job)

    return result


# ---------------------------------------------------------------------------
# Chained entry point
# ---------------------------------------------------------------------------

def apply_hard_filters(jobs: list[dict], profile: UserProfile) -> list[dict]:
    """Apply all hard filters in sequence: job_type -> sponsorship -> location.

    Accepts a list of job dicts and a UserProfile with filter preferences.
    Returns the filtered list. Empty input produces empty output.
    No DB dependency — pure Python.
    """
    original_count = len(jobs)

    after_type = filter_by_job_type(jobs, profile.preferred_job_type)
    result = filter_by_sponsorship(after_type, profile.requires_sponsorship)
    # Location is intentionally NOT a hard filter — it's a scoring signal.
    # Users should see all relevant jobs; location preference boosts score
    # via location_fit in scorer.py, but doesn't eliminate results.

    logger.debug("Hard filters: {} -> {} jobs", original_count, len(result))

    # Runtime gate / observability: hard filters wiping out (nearly) the entire
    # candidate pool is the dominant "no/irrelevant matches" failure mode. Emit
    # a WARNING with the per-stage counts so it is diagnosable from logs on the
    # very call that produced bad results — instead of being silently invisible.
    if original_count > 0 and len(result) == 0:
        logger.warning(
            "Hard filters eliminated ALL {} candidates "
            "(after_job_type={}, after_sponsorship={}; "
            "job_type={}, requires_sponsorship={}, user_id={})",
            original_count,
            len(after_type),
            len(result),
            profile.preferred_job_type.value,
            profile.requires_sponsorship,
            profile.user_id,
        )
    elif (
        profile.requires_sponsorship
        and len(after_type) > 0
        and len(result) / len(after_type) < 0.1
    ):
        logger.warning(
            "Sponsorship hard filter removed {:.0%} of candidates "
            "({} -> {}); most jobs may have unknown sponsorship (user_id={})",
            1 - len(result) / len(after_type),
            len(after_type),
            len(result),
            profile.user_id,
        )

    return result
