"""Pure URL routing for ATS JD fetch tiers.

Phase 14 establishes deterministic routing before any network I/O:
- Greenhouse
- Lever
- Ashby
- Workday
- Firecrawl fallback
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlparse, urlunparse


class FetchRoute(StrEnum):
    """Known JD fetch routes."""

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    FIRECRAWL = "firecrawl"


@dataclass(frozen=True, slots=True)
class UrlClassification:
    """Routing result for one job URL."""

    route: FetchRoute
    normalized_url: str
    hostname: str


def normalize_job_url(url: str) -> str:
    """Normalize a job URL for routing comparisons.

    Removes query strings and fragments so tracking parameters cannot
    change the ATS route classification.
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw)
    if not parsed.scheme:
        parsed = urlparse(f"https://{raw}")

    clean = parsed._replace(
        scheme=(parsed.scheme or "https").lower(),
        netloc=parsed.netloc.lower(),
        path=parsed.path.rstrip("/") or "/",
        params="",
        query="",
        fragment="",
    )
    return urlunparse(clean)


def classify_job_url(url: str) -> UrlClassification:
    """Classify a job URL into the correct ATS tier with no I/O."""
    normalized = normalize_job_url(url)
    if not normalized:
        return UrlClassification(
            route=FetchRoute.FIRECRAWL,
            normalized_url="",
            hostname="",
        )

    parsed = urlparse(normalized)
    hostname = parsed.netloc.lower()
    path = parsed.path.lower()

    if "greenhouse.io" in hostname:
        return UrlClassification(FetchRoute.GREENHOUSE, normalized, hostname)

    if "lever.co" in hostname:
        return UrlClassification(FetchRoute.LEVER, normalized, hostname)

    if "ashbyhq.com" in hostname:
        return UrlClassification(FetchRoute.ASHBY, normalized, hostname)

    if "myworkdayjobs.com" in hostname or "myworkdaysite.com" in hostname:
        return UrlClassification(FetchRoute.WORKDAY, normalized, hostname)

    if "/wday/" in path or "/job/" in path and "workday" in hostname:
        return UrlClassification(FetchRoute.WORKDAY, normalized, hostname)

    return UrlClassification(FetchRoute.FIRECRAWL, normalized, hostname)
