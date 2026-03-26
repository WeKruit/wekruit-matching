"""Stable job ID and content hash utilities.

Provides deterministic, emoji-safe ID generation for job listings scraped
from SimplifyJobs GitHub README tables.

Key problem this solves: SimplifyJobs frequently adds/removes decorative emoji
from company names (e.g., "🔥 Google" → "Google"). Without normalization,
every emoji change generates a new job_id, causing duplicate rows and wasted
LLM enrichment calls.

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
    # Remove all emoji and other non-text Unicode categories (So, Sm, Sk, Cs, Cn)
    cleaned = "".join(
        ch for ch in raw
        if unicodedata.category(ch) not in ("So", "Sm", "Sk", "Cs", "Cn")
    )
    # Lowercase, strip punctuation (keep letters, digits, spaces)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned.lower())
    # Collapse whitespace
    return " ".join(cleaned.split())


def generate_job_id(company_name: str, role_title: str, primary_url: str) -> str:
    """Generate a stable 64-char SHA-256 job ID from normalized fields.

    Normalization ensures emoji variations ("🔥 Google" vs "Google") produce
    the same ID. Per SCRP-06.

    The ID is stable across scrape runs as long as company name (after
    normalization), role title, and primary URL remain unchanged.

    Args:
        company_name: Raw or normalized company name.
        role_title: Job role title (e.g., "Software Engineer Intern").
        primary_url: The primary application URL for this listing.

    Returns:
        64-character lowercase SHA-256 hex string matching r"[0-9a-f]{64}".
    """
    normalized_key = "|".join([
        normalize_company_name(company_name),
        role_title.strip().lower(),
        primary_url.strip().lower(),
    ])
    return hashlib.sha256(normalized_key.encode("utf-8")).hexdigest()


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
