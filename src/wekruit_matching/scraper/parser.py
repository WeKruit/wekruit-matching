"""SimplifyJobs README parser.

Parses SimplifyJobs GitHub README markdown tables into Job objects.

Handles three edge cases that break naive parsers:
- HTML-embedded multi-location cells (<details><summary> blocks) — SCRP-03
- Closed listings (🔒 in Company column) excluded from output — SCRP-04
- Continuation rows (↳) inheriting company from prior non-continuation row — SCRP-05

Usage:
    from wekruit_matching.scraper.parser import parse_readme

    content = Path("README.md").read_bytes()
    jobs = parse_readme(content, "Summer2026-Internships")
"""
import re
from html.parser import HTMLParser
from typing import Optional

from loguru import logger

from wekruit_matching.models.job import Job, JobStatus
from wekruit_matching.scraper.id_utils import (
    compute_content_hash,
    generate_job_id,
    normalize_company_name,
)


class _HTMLStripper(HTMLParser):
    """Extract plain text from HTML fragments in table cells.

    Accumulates all text nodes encountered. Used to strip <details>, <summary>,
    <strong>, <br>, and HTML anchor tags from location and company cells.
    """

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    def get_text(self) -> str:
        """Return all collected text nodes joined by ', '."""
        return ", ".join(self._parts)


def _strip_html(cell: str) -> str:
    """Remove HTML tags from a table cell, returning plain text.

    For <details><summary>N locations</summary>loc1<br>loc2</details>,
    returns "loc1, loc2" (the summary summary-line "N locations" is
    stripped out since it is a redundant count artifact, not a location).

    Args:
        cell: Raw table cell content (may contain HTML).

    Returns:
        Plain text with HTML stripped. Falls back to raw cell if parsing
        yields empty string.
    """
    stripper = _HTMLStripper()
    stripper.feed(cell)
    text = stripper.get_text()
    # Remove the "N locations" summary artifact if present (e.g., "3 locations, ")
    text = re.sub(r"\d+\s+locations?,?\s*", "", text, flags=re.IGNORECASE).strip(", ")
    return text or cell.strip()


def _extract_url(cell: str) -> Optional[str]:
    """Extract first href from a markdown link or HTML anchor in a cell.

    Handles:
    - Markdown link: [text](url)
    - HTML anchor: href="url"

    Returns first https:// URL found, or None if no URL is present.
    """
    # Markdown link: [text](url)
    md_match = re.search(r"\[.*?\]\((https?://[^\)]+)\)", cell)
    if md_match:
        return md_match.group(1)
    # HTML anchor: href="url"
    html_match = re.search(r'href="(https?://[^"]+)"', cell)
    if html_match:
        return html_match.group(1)
    return None


_LOCK_EMOJI = "\U0001F512"  # 🔒
_CONTINUATION_MARKER = "\u21B3"  # ↳


def _is_closed(company_cell: str) -> bool:
    """Return True if this row is a closed/locked listing (🔒 in company column)."""
    return _LOCK_EMOJI in company_cell


def _is_continuation(company_cell: str) -> bool:
    """Return True if this row is a continuation row (starts with ↳)."""
    return company_cell.strip().startswith(_CONTINUATION_MARKER)


def _parse_company_name(cell: str) -> str:
    """Extract clean, normalized company name from a markdown table cell.

    Strips markdown link syntax, HTML tags, and emoji. Returns lowercase
    normalized string suitable for use in ID generation.

    Args:
        cell: Raw company column content (e.g., "[Acme Corp](https://acme.com)").

    Returns:
        Normalized company name (lowercase, no emoji, no punctuation extras).
    """
    # Strip markdown link syntax: [Name](url) -> Name
    name = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", cell)
    # Strip remaining HTML tags
    name = _strip_html(name)
    # Normalize: strip emoji, lowercase, collapse whitespace
    return normalize_company_name(name)


def parse_readme(content: bytes, source_repo: str) -> list[Job]:
    """Parse SimplifyJobs README markdown bytes into a list of Job objects.

    Reads the markdown table line by line, skipping:
    - The header row (Company, Role, Location, ...)
    - The separator row (--- | --- | ...)
    - Closed listings (🔒 in Company column) — per SCRP-04
    - Rows that produce an empty company name after normalization

    Handles:
    - HTML-embedded multi-location cells (<details>/<summary>) — per SCRP-03
    - Continuation rows (↳) inheriting company from the most recent
      non-continuation row — per SCRP-05

    Args:
        content: Raw bytes of the README markdown file.
        source_repo: Repository slug (e.g., "Summer2026-Internships" or
            "New-Grad-Positions"). Stored verbatim in Job.source_repo.

    Returns:
        List of Job objects with job_id, content_hash, and source_repo
        populated. All jobs have status=ACTIVE — the upsert layer handles
        staleness detection.
    """
    text = content.decode("utf-8", errors="replace")
    lines = text.splitlines()

    jobs: list[Job] = []
    last_company: str = ""  # Tracks company name for ↳ continuation rows
    in_table: bool = False
    header_seen: bool = False
    separator_seen: bool = False

    for line in lines:
        stripped = line.strip()

        # Lines not starting with | are not part of a table
        if not stripped.startswith("|"):
            # Reset table state — each table is independent
            in_table = False
            header_seen = False
            separator_seen = False
            continue

        # Split on | and strip whitespace from each cell.
        # "| col1 | col2 |" splits as ['', ' col1 ', ' col2 ', '']
        # We drop the leading and trailing empty strings from boundary pipes.
        raw_cells = stripped.split("|")
        cells = [c.strip() for c in raw_cells[1:-1]]

        if not cells:
            continue

        # Detect header row: contains "Company" or "Role" (case-insensitive)
        if any(c.lower() in ("company", "role", "location") for c in cells):
            in_table = True
            header_seen = True
            separator_seen = False
            continue

        # Detect separator row: all non-empty cells match "---" pattern
        if header_seen and all(re.match(r"-+", c) for c in cells if c):
            separator_seen = True
            continue

        # Only process rows inside a fully initialized table
        if not (in_table and header_seen and separator_seen):
            continue

        # Need at least 3 cells: company, role, location (link and date are optional)
        if len(cells) < 3:
            logger.debug("Skipping row with fewer than 3 cells: {}", stripped[:80])
            continue

        company_cell = cells[0]
        role_cell = cells[1]
        location_cell = cells[2]
        link_cell = cells[3] if len(cells) > 3 else ""
        date_cell = cells[4] if len(cells) > 4 else None

        # Skip closed listings (🔒 in company column) — per SCRP-04
        if _is_closed(company_cell):
            logger.debug("Skipping closed listing: {}", company_cell[:60])
            continue

        # Handle continuation rows (↳) — per SCRP-05
        if _is_continuation(company_cell):
            company_name = last_company
        else:
            company_name = _parse_company_name(company_cell)
            last_company = company_name

        if not company_name:
            logger.warning(
                "Empty company name after normalization, skipping row: {}",
                stripped[:80],
            )
            continue

        # Clean role title — strip markdown link syntax if present
        role_title = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", role_cell).strip()

        # Strip HTML from location if it contains HTML tags
        location_raw = (
            _strip_html(location_cell) if "<" in location_cell else location_cell.strip()
        )

        primary_url = _extract_url(link_cell) if link_cell else None

        job_id = generate_job_id(
            company_name=company_name,
            role_title=role_title,
            primary_url=primary_url or "",
        )
        content_hash = compute_content_hash(
            company_name=company_name,
            role_title=role_title,
        )

        jobs.append(
            Job(
                job_id=job_id,
                source_repo=source_repo,
                company_name=company_name,
                role_title=role_title,
                primary_url=primary_url,
                location_raw=location_raw,
                date_posted_raw=date_cell,
                status=JobStatus.ACTIVE,
                content_hash=content_hash,
            )
        )

    logger.info("Parsed {} active jobs from {} README", len(jobs), source_repo)
    return jobs
