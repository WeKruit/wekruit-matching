"""Free ATS parsers for Greenhouse, Lever, and Ashby.

Phase 15 intentionally keeps this layer narrow:
- normalize text from public ATS payloads
- map ATS-specific fields into one canonical result
- compute data quality at fetch time
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx

from wekruit_matching.pipeline.url_classifier import FetchRoute, classify_job_url, normalize_job_url

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"</?(?:p|div|section|article|li|ul|ol|br|h[1-6])[^>]*>", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")


@dataclass(frozen=True, slots=True)
class AtsJobData:
    """Canonical representation of one ATS job description payload."""

    source: str
    description_plain: str
    department: str | None = None
    location: str | None = None
    workplace_type: str | None = None
    employment_type: str | None = None
    salary_range: str | None = None
    benefits: list[str] = field(default_factory=list)
    qualifications: list[str] = field(default_factory=list)
    core_responsibilities: list[str] = field(default_factory=list)
    published_at: datetime | None = None
    data_quality_score: int = 0


def normalize_text(text: str | None) -> str:
    """Normalize ATS text to plain, whitespace-clean UTF-8-safe text."""
    value = unicodedata.normalize("NFKC", html.unescape(text or ""))
    value = _ZERO_WIDTH_RE.sub("", value)
    value = _BLOCK_TAG_RE.sub(" ", value)
    value = _TAG_RE.sub(" ", value)
    return _WHITESPACE_RE.sub(" ", value).strip()


def calculate_data_quality_score(
    *,
    description_plain: str,
    department: str | None,
    location: str | None,
    employment_type: str | None,
    workplace_type: str | None,
    salary_range: str | None,
    published_at: datetime | None,
) -> int:
    """Compute the Phase 15 quality score using fixed weights."""
    completeness = 0
    for value in (
        description_plain,
        department,
        location,
        employment_type,
        workplace_type,
    ):
        if normalize_text(value):
            completeness += 10

    recency = 0
    if published_at is not None:
        age_days = max((datetime.now(UTC) - published_at).days, 0)
        if age_days <= 30:
            recency = 25
        elif age_days <= 90:
            recency = 15
        elif age_days <= 180:
            recency = 8

    length_score = 0
    length = len(description_plain)
    if length >= 400:
        length_score = 15
    elif length >= 200:
        length_score = 10
    elif length >= 80:
        length_score = 5

    salary_score = 10 if normalize_text(salary_range) else 0
    return min(completeness + recency + length_score + salary_score, 100)


def _parse_datetime(value: str | int | float | None) -> datetime | None:
    """Parse ISO strings or epoch values into UTC datetimes."""
    if value in (None, ""):
        return None

    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp, tz=UTC)

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _format_lever_salary(salary_range: dict | None) -> str | None:
    """Flatten Lever salaryRange objects into one display string."""
    if not isinstance(salary_range, dict):
        return None

    currency = normalize_text(str(salary_range.get("currency") or ""))
    minimum = salary_range.get("min")
    maximum = salary_range.get("max")
    interval = normalize_text(str(salary_range.get("interval") or ""))
    if minimum is None and maximum is None:
        return None
    return normalize_text(f"{currency} {minimum}-{maximum} {interval}")


def _extract_greenhouse_salary(metadata: object) -> str | None:
    """Best-effort extraction of salary metadata from Greenhouse custom fields."""
    if isinstance(metadata, dict):
        items = metadata.items()
    elif isinstance(metadata, list):
        items = [
            (
                str(item.get("name") or item.get("label") or ""),
                item.get("value"),
            )
            for item in metadata
            if isinstance(item, dict)
        ]
    else:
        return None

    for name, value in items:
        label = normalize_text(name).lower()
        if "salary" not in label and "compensation" not in label:
            continue
        if isinstance(value, list):
            return normalize_text(", ".join(str(part) for part in value if part))
        return normalize_text(str(value))
    return None


def build_ats_job_data(
    *,
    source: str,
    description_plain: str,
    department: str | None = None,
    location: str | None = None,
    workplace_type: str | None = None,
    employment_type: str | None = None,
    salary_range: str | None = None,
    benefits: list[str] | None = None,
    qualifications: list[str] | None = None,
    core_responsibilities: list[str] | None = None,
    published_at: datetime | None = None,
) -> AtsJobData:
    """Construct the canonical ATS result and compute quality score."""
    clean_description = normalize_text(description_plain)
    clean_department = normalize_text(department) or None
    clean_location = normalize_text(location) or None
    clean_workplace_type = normalize_text(workplace_type) or None
    clean_employment_type = normalize_text(employment_type) or None
    clean_salary_range = normalize_text(salary_range) or None
    clean_benefits = [text for item in benefits or [] if (text := normalize_text(item))]
    clean_qualifications = [
        text for item in qualifications or [] if (text := normalize_text(item))
    ]
    clean_responsibilities = [
        text for item in core_responsibilities or [] if (text := normalize_text(item))
    ]

    return AtsJobData(
        source=source,
        description_plain=clean_description,
        department=clean_department,
        location=clean_location,
        workplace_type=clean_workplace_type,
        employment_type=clean_employment_type,
        salary_range=clean_salary_range,
        benefits=clean_benefits,
        qualifications=clean_qualifications,
        core_responsibilities=clean_responsibilities,
        published_at=published_at,
        data_quality_score=calculate_data_quality_score(
            description_plain=clean_description,
            department=clean_department,
            location=clean_location,
            employment_type=clean_employment_type,
            workplace_type=clean_workplace_type,
            salary_range=clean_salary_range,
            published_at=published_at,
        ),
    )


def _resolve_greenhouse_embed(
    url: str, *, client: httpx.Client
) -> tuple[str, str]:
    """Resolve a Greenhouse ``boards.greenhouse.io/embed/job_app?token=N`` URL
    to ``(board_token, job_id)``.

    Boards in this shape (commonly emitted by Simplify and similar
    aggregators) carry only the job id in the query string. Greenhouse
    301-redirects them to ``job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>``
    where ``for`` is the board token. Following the redirect once is the
    cheapest way to recover both pieces — no extra API needed.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    token_values = qs.get("token") or qs.get("for")
    if not token_values:
        raise ValueError(f"Unsupported Greenhouse job URL: {url}")
    job_id = token_values[0]

    # Some inbound URLs already include for=<board>&token=<id>
    for_values = qs.get("for")
    if for_values:
        return for_values[0], job_id

    response = client.get(url)
    final_url = str(response.url)
    final_qs = parse_qs(urlparse(final_url).query)
    for_values = final_qs.get("for")
    if not for_values:
        raise ValueError(f"Unsupported Greenhouse job URL: {url}")
    return for_values[0], (final_qs.get("token") or [job_id])[0]


def fetch_greenhouse_job(url: str, *, client: httpx.Client | None = None) -> AtsJobData:
    """Fetch one Greenhouse job through the public board API.

    Supports two URL shapes:
      1. ``boards.greenhouse.io/<board>/jobs/<id>`` — standard board page.
      2. ``boards.greenhouse.io/embed/job_app?token=<id>`` — embed widget
         used by Simplify and other aggregators (P7-L, 2026-05-08). We
         follow Greenhouse's 301 to ``job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>``
         to recover the board slug, then call the public boards-api as
         usual.
    """
    normalized = normalize_job_url(url)
    parsed = urlparse(normalized)
    path_parts = [part for part in parsed.path.split("/") if part]

    owns_client = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=True)
    try:
        if path_parts[:2] == ["embed", "job_app"] or (
            len(path_parts) >= 1 and path_parts[0] == "embed"
        ):
            # ``normalized`` strips query strings; the embed pattern carries
            # the job id in the query, so pass the raw url to the resolver.
            board_token, job_id = _resolve_greenhouse_embed(url, client=client)
        elif len(path_parts) >= 3 and path_parts[1] == "jobs":
            board_token = path_parts[0]
            job_id = path_parts[2]
        else:
            raise ValueError(f"Unsupported Greenhouse job URL: {url}")

        api_url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}?content=true"
        response = client.get(api_url)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            client.close()

    departments = payload.get("departments") or []
    offices = payload.get("offices") or []
    return build_ats_job_data(
        source=FetchRoute.GREENHOUSE.value,
        description_plain=str(payload.get("content") or ""),
        department=str(departments[0].get("name") or "") if departments else None,
        location=str(offices[0].get("name") or "") if offices else None,
        salary_range=_extract_greenhouse_salary(payload.get("metadata")),
        published_at=_parse_datetime(payload.get("updated_at")),
    )


def fetch_lever_job(url: str, *, client: httpx.Client | None = None) -> AtsJobData:
    """Fetch one Lever job through the public postings API."""
    normalized = normalize_job_url(url)
    parsed = urlparse(normalized)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        raise ValueError(f"Unsupported Lever job URL: {url}")

    api_host = "api.eu.lever.co" if ".eu.lever.co" in parsed.netloc.lower() else "api.lever.co"
    site, posting_id = path_parts[0], path_parts[1]
    api_url = f"https://{api_host}/v0/postings/{site}/{posting_id}"

    owns_client = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=True)
    try:
        response = client.get(api_url)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            client.close()

    responsibilities: list[str] = []
    qualifications: list[str] = []
    benefits: list[str] = []
    for item in payload.get("lists") or []:
        if not isinstance(item, dict):
            continue
        label = normalize_text(str(item.get("text") or "")).lower()
        content = item.get("content") or []
        if not isinstance(content, list):
            content = [content]
        values = [str(entry) for entry in content if entry]
        if "responsibil" in label:
            responsibilities.extend(values)
        elif "requirement" in label or "qualification" in label:
            qualifications.extend(values)
        elif "benefit" in label:
            benefits.extend(values)

    categories = payload.get("categories") or {}
    return build_ats_job_data(
        source=FetchRoute.LEVER.value,
        description_plain=str(payload.get("descriptionPlain") or ""),
        department=str(categories.get("team") or ""),
        location=str(categories.get("location") or ""),
        workplace_type=str(payload.get("workplaceType") or ""),
        employment_type=str(categories.get("commitment") or ""),
        salary_range=_format_lever_salary(payload.get("salaryRange")),
        benefits=benefits,
        qualifications=qualifications,
        core_responsibilities=responsibilities,
        published_at=_parse_datetime(payload.get("createdAt")),
    )


def fetch_ashby_job(url: str, *, client: httpx.Client | None = None) -> AtsJobData:
    """Fetch one Ashby job by resolving its hosted URL within the public job board feed."""
    normalized = normalize_job_url(url)
    parsed = urlparse(normalized)
    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 2:
        raise ValueError(f"Unsupported Ashby job URL: {url}")

    board_name = path_parts[0]
    api_url = (
        f"https://api.ashbyhq.com/posting-api/job-board/{board_name}?includeCompensation=true"
    )

    owns_client = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=True)
    try:
        response = client.get(api_url)
        response.raise_for_status()
        payload = response.json()
    finally:
        if owns_client:
            client.close()

    for job in payload.get("jobs") or []:
        if not isinstance(job, dict):
            continue
        candidates = [
            normalize_job_url(str(job.get("jobUrl") or "")),
            normalize_job_url(str(job.get("applyUrl") or "")),
        ]
        if normalized not in candidates:
            continue
        compensation = job.get("compensation") or {}
        return build_ats_job_data(
            source=FetchRoute.ASHBY.value,
            description_plain=str(job.get("descriptionPlain") or job.get("descriptionHtml") or ""),
            department=str(job.get("department") or job.get("team") or ""),
            location=str(job.get("location") or ""),
            workplace_type=str(job.get("workplaceType") or ""),
            employment_type=str(job.get("employmentType") or ""),
            salary_range=str(
                compensation.get("scrapeableCompensationSalarySummary")
                or compensation.get("compensationTierSummary")
                or ""
            ),
            published_at=_parse_datetime(job.get("publishedAt")),
        )

    raise LookupError(f"Could not find matching Ashby posting for {url}")


def fetch_free_ats_job(url: str, *, client: httpx.Client | None = None) -> AtsJobData:
    """Dispatch to the correct free ATS parser based on the Phase 14 router."""
    route = classify_job_url(url).route
    if route is FetchRoute.GREENHOUSE:
        return fetch_greenhouse_job(url, client=client)
    if route is FetchRoute.LEVER:
        return fetch_lever_job(url, client=client)
    if route is FetchRoute.ASHBY:
        return fetch_ashby_job(url, client=client)
    raise ValueError(f"Unsupported free ATS route for {url}: {route.value}")
