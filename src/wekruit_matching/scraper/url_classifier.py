"""Pure-regex URL classifier for routing job URLs to ATS parser tiers.

No I/O — safe to call in tight loops. classify() is the primary public API.

Every ATS parser in Phase 15 imports classify() to know which fetch path to
take. The URL classifier must have zero I/O — called thousands of times per
pipeline run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ATSTier(str, Enum):
    JOBRIGHT = "jobright"
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    SIMPLIFY = "simplify"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedURL:
    tier: ATSTier
    raw_url: str
    slug: str | None  # extracted ATS company slug when detectable from URL


# Compiled patterns — loaded once at module import, zero I/O
_GREENHOUSE_PATTERNS = [
    re.compile(r"boards\.greenhouse\.io/([^/?#]+)", re.IGNORECASE),
    re.compile(r"job-boards\.greenhouse\.io/([^/?#]+)", re.IGNORECASE),
    re.compile(r"([a-z0-9_-]+)\.greenhouse\.io", re.IGNORECASE),
]

_LEVER_PATTERNS = [
    re.compile(r"jobs\.lever\.co/([^/?#]+)", re.IGNORECASE),
    re.compile(r"([a-z0-9_-]+)\.lever\.co", re.IGNORECASE),
]

_ASHBY_PATTERNS = [
    re.compile(r"jobs\.ashbyhq\.com/([^/?#]+)", re.IGNORECASE),
    re.compile(r"([a-z0-9_-]+)\.ashbyhq\.com", re.IGNORECASE),
]

_WORKDAY_PATTERNS = [
    re.compile(r"([a-z0-9_-]+)\.myworkdayjobs\.com", re.IGNORECASE),
    re.compile(r"wd\d+\.myworkdayjobs\.com", re.IGNORECASE),
]

_JOBRIGHT_PATTERNS = [
    re.compile(r"jobright\.ai", re.IGNORECASE),
]

_SIMPLIFY_PATTERNS = [
    re.compile(r"simplify\.jobs", re.IGNORECASE),
]

# Ordered list of (tier, patterns) — first match wins
_TIER_PATTERNS: list[tuple[ATSTier, list[re.Pattern[str]]]] = [
    (ATSTier.GREENHOUSE, _GREENHOUSE_PATTERNS),
    (ATSTier.LEVER, _LEVER_PATTERNS),
    (ATSTier.ASHBY, _ASHBY_PATTERNS),
    (ATSTier.WORKDAY, _WORKDAY_PATTERNS),
    (ATSTier.JOBRIGHT, _JOBRIGHT_PATTERNS),
    (ATSTier.SIMPLIFY, _SIMPLIFY_PATTERNS),
]


def classify(url: str) -> ClassifiedURL:
    """Classify a job URL to its ATS tier using regex matching.

    Pure function — no I/O, no side effects. Safe to call in tight loops.
    Tries each ATS group in order (Greenhouse, Lever, Ashby, Workday,
    JobRight, Simplify). First match wins. Slug is extracted from capture
    group 1 where available.

    Args:
        url: Raw job URL string.

    Returns:
        ClassifiedURL with tier, raw_url, and slug (or None).
    """
    for tier, patterns in _TIER_PATTERNS:
        for pattern in patterns:
            match = pattern.search(url)
            if match:
                slug: str | None = None
                if match.lastindex and match.lastindex >= 1:
                    captured = match.group(1)
                    # For Workday wd\d+ patterns, there's no meaningful slug
                    if captured and not re.match(r"^wd\d+$", captured, re.IGNORECASE):
                        slug = captured
                return ClassifiedURL(tier=tier, raw_url=url, slug=slug)

    return ClassifiedURL(tier=ATSTier.UNKNOWN, raw_url=url, slug=None)
