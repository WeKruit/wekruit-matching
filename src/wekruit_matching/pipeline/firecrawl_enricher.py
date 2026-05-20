"""Workday and Firecrawl enrichment helpers for the JD pipeline."""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from wekruit_matching.pipeline.ats_enricher import AtsJobData, build_ats_job_data, normalize_text
from wekruit_matching.pipeline.url_classifier import normalize_job_url

_WORKDAY_CXS_RE = re.compile(r"/wday/cxs/([^/]+)/([^/]+)/jobs", re.IGNORECASE)
_AGGREGATOR_HOSTS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "simplyhired.com",
)
_JD_KEYWORDS = (
    "responsibilities",
    "requirements",
    "qualifications",
    "experience",
    "skills",
    "what you'll do",
    "about the role",
)


@dataclass(frozen=True, slots=True)
class FirecrawlFetchResult:
    """Result from the Firecrawl fallback chain."""

    job_data: AtsJobData
    credits_used: int
    resolved_url: str


async def run_with_timeout(awaitable, *, timeout_seconds: float):
    """Enforce an asyncio-level timeout around I/O."""
    return await asyncio.wait_for(awaitable, timeout=timeout_seconds)


def _has_jd_content(markdown: str | None) -> bool:
    """Heuristic: markdown is a real JD only if it is substantive and job-like."""
    text = normalize_text(markdown)
    if len(text) < 200:
        return False
    lowered = text.lower()
    return any(keyword in lowered for keyword in _JD_KEYWORDS)


def _firecrawl_base_url(base_url: str) -> str:
    """Normalize the Firecrawl base URL to an API endpoint root.

    Self-hosted uses /v1, cloud uses /v2. Detect based on hostname.
    """
    normalized = base_url.rstrip("/")
    if normalized.endswith(("/v1", "/v2")):
        return normalized
    # Self-hosted (localhost or custom domain) → v1
    if "localhost" in normalized or "127.0.0.1" in normalized:
        return f"{normalized}/v1"
    # Cloud API → v1 (v2 deprecated in newer versions)
    return f"{normalized}/v1"


def _extract_firecrawl_data(payload: dict) -> dict:
    """Support both top-level and nested Firecrawl response shapes."""
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first
    return payload


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
    json: dict | None = None,
) -> dict:
    """Send one JSON request with an asyncio-level timeout."""
    response = await run_with_timeout(
        client.request(method, url, headers=headers, json=json),
        timeout_seconds=timeout_seconds,
    )
    response.raise_for_status()
    return response.json()


async def discover_workday_cxs_endpoint(
    url: str,
    *,
    client: httpx.AsyncClient,
    timeout_seconds: float = 90.0,
) -> tuple[str, str]:
    """Resolve the Workday tenant/site pair from the hosted job page."""
    response = await run_with_timeout(
        client.get(normalize_job_url(url), follow_redirects=True),
        timeout_seconds=timeout_seconds,
    )
    response.raise_for_status()

    match = _WORKDAY_CXS_RE.search(response.text)
    if match:
        return match.group(1), match.group(2)

    path_parts = [part for part in urlparse(normalize_job_url(url)).path.split("/") if part]
    try:
        recruiting_index = path_parts.index("recruiting")
    except ValueError as exc:
        raise LookupError(f"Could not discover Workday CXS endpoint for {url}") from exc

    if len(path_parts) <= recruiting_index + 2:
        raise LookupError(f"Could not discover Workday CXS endpoint for {url}")
    return path_parts[recruiting_index + 1], path_parts[recruiting_index + 2]


def _select_workday_posting(payload: dict, normalized_url: str) -> dict | None:
    """Find the Workday posting whose external path matches the hosted URL."""
    postings = (
        payload.get("jobPostings")
        or payload.get("jobPostingsList")
        or payload.get("jobs")
        or []
    )
    parsed_url = urlparse(normalized_url)
    for posting in postings:
        if not isinstance(posting, dict):
            continue
        external_path = str(posting.get("externalPath") or "")
        if not external_path:
            continue
        if parsed_url.path.endswith(external_path) or external_path in parsed_url.path:
            return posting
    return None


async def fetch_workday_job(
    url: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 90.0,
) -> AtsJobData | None:
    """Fetch one Workday job through the CXS jobs endpoint."""
    normalized = normalize_job_url(url)
    parsed = urlparse(normalized)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        tenant, site = await discover_workday_cxs_endpoint(
            normalized,
            client=client,
            timeout_seconds=timeout_seconds,
        )
        endpoint = f"{parsed.scheme}://{parsed.netloc}/wday/cxs/{tenant}/{site}/jobs"
        payload = await _request_json(
            client,
            "POST",
            endpoint,
            timeout_seconds=timeout_seconds,
            json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""},
        )
    finally:
        if owns_client:
            await client.aclose()

    posting = _select_workday_posting(payload, normalized)
    if posting is None:
        return None

    description = (
        posting.get("jobDescription")
        or posting.get("description")
        or posting.get("externalDescription")
        or ""
    )
    return build_ats_job_data(
        source="workday",
        description_plain=str(description),
        location=str(
            posting.get("locationsText")
            or posting.get("location")
            or ""
        ),
        published_at=None,
    )


async def fetch_firecrawl_job(
    url: str,
    *,
    api_key: str,
    base_url: str = "https://api.firecrawl.dev",
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 90.0,
) -> FirecrawlFetchResult | None:
    """Try Firecrawl scrape first and escalate to extract only when necessary."""
    headers = {"Authorization": f"Bearer {api_key}"}
    root = _firecrawl_base_url(base_url)
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        scrape_payload = await _request_json(
            client,
            "POST",
            f"{root}/scrape",
            timeout_seconds=timeout_seconds,
            headers=headers,
            # waitFor 5000 → 20000 (2026-05-20, matching-quality launch
            # blocker). 5s was too short for SPA pages on Workday / some
            # Lever / some Greenhouse — Playwright would load 100KB+ of HTML
            # at status 200 with no error, but Firecrawl declared the page
            # "not long enough" because the JD body was still hydrating in
            # JS DOM that the static markdown extractor cannot reach. 20s
            # is the upper bound observed for these SPAs to finish painting.
            json={"url": url, "formats": ["markdown"], "waitFor": 20000},
        )
        scrape_data = _extract_firecrawl_data(scrape_payload)
        markdown = str(scrape_data.get("markdown") or "")
        if _has_jd_content(markdown):
            return FirecrawlFetchResult(
                job_data=build_ats_job_data(
                    source="firecrawl",
                    description_plain=markdown,
                ),
                credits_used=1,
                resolved_url=normalize_job_url(url),
            )

        extract_payload = await _request_json(
            client,
            "POST",
            f"{root}/extract",
            timeout_seconds=timeout_seconds,
            headers=headers,
            json={
                "urls": [url],
                "schema": {
                    "type": "object",
                    "properties": {
                        "job_description": {"type": "string"},
                        "responsibilities": {"type": "array", "items": {"type": "string"}},
                        "qualifications": {"type": "array", "items": {"type": "string"}},
                        "salary_range": {"type": "string"},
                    },
                },
            },
        )
    finally:
        if owns_client:
            await client.aclose()

    extract_data = _extract_firecrawl_data(extract_payload)
    description = str(
        extract_data.get("job_description")
        or extract_data.get("description")
        or ""
    )
    if not normalize_text(description):
        return None

    return FirecrawlFetchResult(
        job_data=build_ats_job_data(
            source="firecrawl",
            description_plain=description,
            salary_range=str(extract_data.get("salary_range") or ""),
            qualifications=[
                str(item) for item in extract_data.get("qualifications") or [] if item
            ],
            core_responsibilities=[
                str(item)
                for item in extract_data.get("responsibilities") or []
                if item
            ],
        ),
        credits_used=6,
        resolved_url=normalize_job_url(url),
    )


async def search_canonical_job_url(
    *,
    company_name: str,
    role_title: str,
    api_key: str,
    base_url: str = "https://api.firecrawl.dev",
    client: httpx.AsyncClient | None = None,
    timeout_seconds: float = 90.0,
) -> str | None:
    """Search for a canonical employer job URL, skipping known aggregators."""
    headers = {"Authorization": f"Bearer {api_key}"}
    query = f"{company_name} {role_title} careers"
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        payload = await _request_json(
            client,
            "POST",
            f"{_firecrawl_base_url(base_url)}/search",
            timeout_seconds=timeout_seconds,
            headers=headers,
            json={"query": query, "limit": 5},
        )
    finally:
        if owns_client:
            await client.aclose()

    results = payload.get("data") or payload.get("results") or []
    for result in results:
        if not isinstance(result, dict):
            continue
        url = normalize_job_url(str(result.get("url") or ""))
        hostname = urlparse(url).netloc.lower()
        if not url or any(host in hostname for host in _AGGREGATOR_HOSTS):
            continue
        return url
    return None
