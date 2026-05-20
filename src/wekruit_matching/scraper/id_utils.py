"""Stable job ID and content hash utilities (v2 — URL-free).

Provides deterministic, emoji-safe ID generation for job listings.

**v2 (2026-05-13) — Adam directive**: drop `primary_url` from the
`generate_job_id` hash. jobright-ai rotates its redirect-URL hex IDs every
time it rewrites its public GitHub README (Walgreens "Shift Lead" alone
landed 13 phantom dupes in Firestore — same contentHash, 13 different
URLs, 13 different job_ids). Adding `source_repo` to the hash preserves
cross-source uniqueness without depending on volatile fields.

Migration: see `scripts/dedupe_jobs.py` — collapses pre-v2 dupes in
Postgres and re-hashes surviving rows to the v2 scheme.

Key normalization problem this still solves: SimplifyJobs frequently
adds/removes decorative emoji from company names (e.g., "🔥 Google" →
"Google"). Without normalization, every emoji change generates a new
job_id, causing duplicate rows and wasted LLM enrichment calls.

Usage:
    from wekruit_matching.scraper.id_utils import (
        generate_job_id,
        compute_content_hash,
        normalize_company_name,
    )
"""
import hashlib
import re
import unicodedata


def normalize_company_name(raw: str) -> str:
    """Strip decorative emoji, lowercase, remove punctuation, collapse whitespace.

    Used before hashing to prevent stable ID breaks when SimplifyJobs changes
    emoji usage (e.g., "🔥 Google" and "Google" must produce the same ID).
    Also handles ↳ continuation rows: caller must pass the inherited parent
    company name, not the raw "↳" string.

    Strategy: use unicodedata category to identify emoji and symbol characters
    (categories So, Sm, Sk, Cs, Cn) — avoids a hardcoded emoji blocklist that
    would require constant maintenance as new emoji are added.

    Args:
        raw: Raw company name string from README table cell.

    Returns:
        Normalized lowercase string with emoji, punctuation, and extra
        whitespace removed.
    """
    cleaned = "".join(
        ch for ch in raw
        if unicodedata.category(ch) not in ("So", "Sm", "Sk", "Cs", "Cn")
    )
    cleaned = re.sub(r"[^\w\s]", " ", cleaned.lower())
    return " ".join(cleaned.split())


def generate_job_id(source_repo: str, company_name: str, role_title: str) -> str:
    """Generate a stable 64-char SHA-256 job ID from URL-free fields.

    **v2**: URL is no longer part of the hash. Caller must supply
    `source_repo` (e.g., "jobright-newgrad" or "greenhouse:airbnb") so the
    same (company, role) tuple at a different ATS provider does not collide.

    The ID is stable across scrape runs as long as `source_repo`, the
    normalized company name, and the role title remain unchanged. Volatile
    fields (URL with rotating utm/tracking params, location, posted date)
    are intentionally excluded.

    Args:
        source_repo: The pipeline-side source slug (e.g. "jobright-newgrad",
            "greenhouse:airbnb"). Disambiguates cross-source listings.
        company_name: Raw or normalized company name. Normalization is
            applied internally — caller can pass either.
        role_title: Job role title (e.g., "Software Engineer Intern").
            Lowercased + stripped before hashing.

    Returns:
        64-character lowercase SHA-256 hex string matching r"[0-9a-f]{64}".
    """
    normalized_key = "|".join([
        source_repo.strip().lower(),
        normalize_company_name(company_name),
        role_title.strip().lower(),
    ])
    return hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()


def normalize_role_title(raw: str) -> str:
    """Lowercase + collapse decorative chars in a role title.

    Symmetrical to :func:`normalize_company_name` but tuned for titles —
    role titles often contain seniority suffixes (Sr/Sr., Senior), comma
    separators ("Software Engineer, Backend"), and slash variants
    ("SWE/Software Engineer"). We don't try to canonicalize seniority
    (that's an LLM job); we just remove emoji + punctuation and collapse
    whitespace so trivial source-of-truth drift between scrapers ("SWE
    Intern" vs "swe intern" vs "SWE 🔥 Intern") collapses to the same
    signature.
    """
    cleaned = "".join(
        ch for ch in raw
        if unicodedata.category(ch) not in ("So", "Sm", "Sk", "Cs", "Cn")
    )
    cleaned = re.sub(r"[^\w\s]", " ", cleaned.lower())
    return " ".join(cleaned.split())


_LOCATION_REMOTE_MARKERS: frozenset[str] = frozenset({
    "remote",
    "anywhere",
    "us remote",
    "remote us",
    "work from home",
    "wfh",
    "fully remote",
    "remote anywhere",
})


def normalize_location(raw: str | None) -> str:
    """Normalize a location string for canonical-signature use.

    Takes the head of multi-location strings ("San Francisco, CA · Remote" →
    "san francisco"), strips decorative chars, collapses remote markers to
    ``__remote__``, and falls back to ``__no_loc__`` when empty.

    Idempotent: ``normalize_location(normalize_location(x))`` == ``normalize_location(x)``.

    Args:
        raw: Raw location string (or None).

    Returns:
        Normalized lowercase token. Special tokens: ``__no_loc__`` for empty
        input, ``__remote__`` for explicit remote markers.
    """
    if not raw or not raw.strip():
        return "__no_loc__"
    head = re.split(r"[·,;|/]", raw)[0]
    cleaned = "".join(
        ch for ch in head
        if unicodedata.category(ch) not in ("So", "Sm", "Sk", "Cs", "Cn")
    )
    cleaned = re.sub(r"[^\w\s]", " ", cleaned.lower())
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return "__no_loc__"
    if cleaned in _LOCATION_REMOTE_MARKERS:
        return "__remote__"
    return cleaned


def compute_canonical_signature(
    company_name: str,
    role_title: str,
    location_raw: str | None = None,
) -> str:
    """Cross-source dedup signature: sha256(v2::norm(co)::norm(role)::norm(loc)).

    Track E (matching-quality launch blocker, 2026-05-20): cross-source
    overlap is small (~107 active rows when first measured) but real.
    Same role at the same company surfaced through two ATS scrapers
    (greenhouse:acme + jobright-newgrad mirror, for example) produces two
    distinct ``job_id``s because ``source_repo`` is part of
    :func:`generate_job_id`. The canonical signature is the second key:
    same human-meaning role at same company, regardless of source — used
    by ``wekruit-pa``'s ``pa-job-canonical-signature/{sig}`` collection to
    log + skip duplicate writes.

    **v2 (2026-05-20)**: location added to disambiguate multi-posting same
    role at same company. Google "Software Engineer" at SF / NYC / Remote
    are different positions and used to collide on the v1 signature
    (norm(co)::norm(role)). The signature now includes ``normalize_location``
    so the three Google SWE postings produce three distinct signatures.

    The ``v2::`` prefix is hashed in (not just prepended) so the resulting
    sha256 is grep-distinct from v1: v1 entries on PA's
    ``pa-job-canonical-signature/{sig}`` collection won't collide with v2
    writes, and a one-time PA backfill can migrate v1 → v2 without a
    coordinated cutover.

    Differs from :func:`compute_content_hash` deliberately:
      * content_hash is case-sensitive (so "Senior Engineer" vs "senior
        engineer" trigger re-enrichment).
      * canonical_signature is case-insensitive + emoji-stripped (so
        cosmetic source-of-truth drift between scrapers collapses).

    Idempotency: pure function. Same inputs → same output, forever.

    Args:
        company_name: Raw company name from source. Normalized internally.
        role_title: Raw role title from source. Normalized internally.
        location_raw: Raw location string (or None). When None / empty the
            location component collapses to ``__no_loc__`` — backward
            compatible with callers that don't pass location, but those
            calls give up multi-location disambiguation.

    Returns:
        64-character lowercase SHA-256 hex string. Stable as long as the
        normalized (company, role, location) triple is unchanged.
    """
    sig_key = "::".join([
        "v2",
        normalize_company_name(company_name),
        normalize_role_title(role_title),
        normalize_location(location_raw),
    ])
    return hashlib.sha256(sig_key.encode("utf-8")).hexdigest()


def compute_content_hash(company_name: str, role_title: str) -> str:
    """Compute SHA-256 hash of enrichable fields for change detection.

    Only company_name and role_title are used because location_raw and
    date_posted_raw can legitimately change without triggering re-enrichment
    (location is cosmetic, date is a display string). Per SCRP-09.

    Used by Phase 3 enrichment gate: skip LLM call if hash unchanged.

    Args:
        company_name: Raw company name (not normalized — content change
            detection should be sensitive to case/punctuation changes).
        role_title: Job role title.

    Returns:
        64-character lowercase SHA-256 hex string matching r"[0-9a-f]{64}".
    """
    content = "|".join([
        company_name.strip(),
        role_title.strip(),
    ])
    return hashlib.sha256(content.encode("utf-8")).hexdigest()
