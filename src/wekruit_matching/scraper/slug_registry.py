"""Slug registry for company->ATS slug lookups.

Loaded from bundled JSON files downloaded from Feashliaa/job-board-aggregator
(27K+ mappings across Greenhouse, Lever, Ashby, Workday). No network calls at
runtime — all lookups are in-memory dict lookups.

The JSON source files contain arrays of slug strings (not company->slug dicts).
Slugs themselves serve as the normalized company identifiers:
  - Greenhouse/Lever/Ashby: ["stripe", "figma", "notion", ...]
  - Workday: ["stripe|wd5|external_careers", ...] (pipe-delimited tenant|wdN|site)

SlugRegistry.lookup(company_name, ats) normalizes the company name and checks
if a matching slug exists in the registry. For Workday, the tenant portion
(before the first '|') is the matchable identifier.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from wekruit_matching.scraper.url_classifier import ATSTier

_DATA_DIR = Path(__file__).parent / "data"

_ATS_FILE_MAP: dict[ATSTier, str] = {
    ATSTier.GREENHOUSE: "greenhouse_slugs.json",
    ATSTier.LEVER: "lever_slugs.json",
    ATSTier.ASHBY: "ashby_slugs.json",
    ATSTier.WORKDAY: "workday_slugs.json",
}


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation, normalize unicode for fuzzy company matching."""
    name = unicodedata.normalize("NFKC", name).lower()
    name = re.sub(r"[^a-z0-9 ]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _slug_to_normalized(slug: str, ats: ATSTier) -> str:
    """Convert a raw slug string to normalized form for matching.

    For Workday, the slug format is 'tenant|wdN|career-site'; we use the
    tenant portion as the matchable identifier.
    """
    if ats == ATSTier.WORKDAY:
        slug = slug.split("|")[0]
    # Slugs use hyphens/underscores as word separators — normalize to spaces
    slug = re.sub(r"[-_]", " ", slug)
    return _normalize_name(slug)


@dataclass
class SlugRegistry:
    """In-memory registry of company slugs per ATS.

    Internal _data structure: {ats_value: {normalized_key: original_slug}}
    where the normalized_key is derived from the slug for fast lookup.
    """

    _data: dict[str, dict[str, str]] = field(default_factory=dict)

    def lookup(self, company_name: str, ats: ATSTier) -> str | None:
        """Return the ATS slug for a company, or None if not found.

        Tries exact normalized match first. If not found, tries a prefix
        containment check (normalized company name is a substring of a
        registry key). No fuzzy string distance — fast O(1) dict lookup
        for exact, O(n) scan for prefix (n = registry size for this ATS).

        Args:
            company_name: Human-readable company name (e.g. "Stripe", "OpenAI").
            ats: ATS tier to search within.

        Returns:
            Slug string if found, None otherwise.
        """
        ats_registry = self._data.get(ats.value)
        if not ats_registry:
            return None

        normalized = _normalize_name(company_name)

        # Exact normalized match
        if normalized in ats_registry:
            return ats_registry[normalized]

        # Prefix containment: check if normalized name appears in any key
        for key, slug in ats_registry.items():
            if normalized and (normalized in key or key in normalized):
                return slug

        return None

    def lookup_all_ats(self, company_name: str) -> dict[ATSTier, str]:
        """Return all ATS slugs known for this company.

        Args:
            company_name: Human-readable company name.

        Returns:
            Dict of {ATSTier: slug} for all ATS tiers where a match was found.
            Empty dict if none found.
        """
        results: dict[ATSTier, str] = {}
        for ats in _ATS_FILE_MAP:
            slug = self.lookup(company_name, ats)
            if slug is not None:
                results[ats] = slug
        return results

    def contains_slug(self, slug: str, ats: ATSTier) -> bool:
        """Check if a raw slug string exists in the registry for the given ATS.

        Args:
            slug: Raw ATS slug (e.g. "stripe", "figma").
            ats: ATS tier to search within.

        Returns:
            True if the slug is a known entry.
        """
        ats_registry = self._data.get(ats.value)
        if not ats_registry:
            return False
        # Check by normalized slug key
        normalized_slug = _slug_to_normalized(slug, ats)
        return normalized_slug in ats_registry


def _parse_file(path: Path, ats: ATSTier) -> dict[str, str]:
    """Parse a slug JSON file into {normalized_key: original_slug} dict.

    Handles:
    - List format: ["slug1", "slug2", ...] (Greenhouse, Lever, Ashby)
    - Workday list: ["tenant|wdN|site", ...] (pipe-delimited)
    - Dict format: {"company": "slug", ...} (future-proofed)

    Returns:
        Dict mapping normalized key to original slug string.
    """
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    result: dict[str, str] = {}

    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, str):
                continue
            # The original slug is the full entry (including Workday pipe parts)
            original_slug = entry
            normalized_key = _slug_to_normalized(entry, ats)
            if normalized_key:
                result[normalized_key] = original_slug

    elif isinstance(data, dict):
        for company_name, slug in data.items():
            if not isinstance(slug, str):
                continue
            normalized_key = _normalize_name(company_name)
            if normalized_key:
                result[normalized_key] = slug

    return result


def load_registry() -> SlugRegistry:
    """Load all 4 ATS slug files and return a populated SlugRegistry.

    Files are loaded from the bundled data/ directory adjacent to this module.
    No network calls — all data is read from local JSON files at load time.

    Returns:
        SlugRegistry with in-memory lookup tables for Greenhouse, Lever,
        Ashby, and Workday.

    Raises:
        FileNotFoundError: If any of the 4 required JSON files is missing.
    """
    registry_data: dict[str, dict[str, str]] = {}

    for ats, filename in _ATS_FILE_MAP.items():
        file_path = _DATA_DIR / filename
        if not file_path.exists():
            raise FileNotFoundError(
                f"Slug registry file not found: {file_path}. "
                "Run the download script to fetch data files."
            )
        registry_data[ats.value] = _parse_file(file_path, ats)

    return SlugRegistry(_data=registry_data)
