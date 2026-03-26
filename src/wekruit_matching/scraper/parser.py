"""SimplifyJobs README parser.

Parses SimplifyJobs GitHub README HTML tables into Job objects.

The SimplifyJobs repos use HTML <table> elements (not markdown pipe tables).
Each category section (Software Engineering, Data Science, etc.) has its own table.

Handles:
- HTML table rows with <td> cells containing nested <strong>, <a>, <br> tags — SCRP-03
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


class _TableRowExtractor(HTMLParser):
    """Extract rows from HTML <table> elements in SimplifyJobs READMEs.

    SimplifyJobs uses HTML tables (not markdown pipe tables).
    Each category section has its own <table> with <thead> and <tbody>.

    Row format:
        <tr>
          <td>🔥 <strong><a href="...">Company</a></strong></td>
          <td>Role Title 🎓</td>
          <td>City, State<br>City2, State2</td>
          <td><div align="center"><a href="apply_url">...</a></td>
          <td>0d</td>
        </tr>
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._in_tbody = False
        self._in_tr = False
        self._in_td = False
        self._in_thead = False
        self._current_row: list[str] = []
        self._current_cell: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag == "thead":
            self._in_thead = True
        elif tag == "tbody":
            self._in_tbody = True
        elif tag == "tr" and self._in_tbody:
            self._in_tr = True
            self._current_row = []
        elif tag == "td" and self._in_tr:
            self._in_td = True
            self._current_cell = []
        elif tag == "br" and self._in_td:
            self._current_cell.append(", ")
        elif tag == "a" and self._in_td:
            # Preserve href for URL extraction
            for attr_name, attr_val in attrs:
                if attr_name == "href" and attr_val:
                    self._current_cell.append(f'<a href="{attr_val}">')

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "thead":
            self._in_thead = False
        elif tag == "tbody":
            self._in_tbody = False
        elif tag == "tr" and self._in_tr:
            self._in_tr = False
            if self._current_row:
                self.rows.append(self._current_row)
        elif tag == "td" and self._in_td:
            self._in_td = False
            self._current_row.append("".join(self._current_cell))
        elif tag == "a" and self._in_td:
            self._current_cell.append("</a>")

    def handle_data(self, data: str) -> None:
        if self._in_td:
            self._current_cell.append(data)


def parse_readme(content: bytes, source_repo: str) -> list[Job]:
    """Parse SimplifyJobs README HTML tables into a list of Job objects.

    SimplifyJobs repos use HTML <table> elements organized by category
    (Software Engineering, Data Science, etc.). Each table has rows with
    5 columns: Company, Role, Location, Application, Age.

    Skips:
    - Closed listings (🔒 in Company column) — per SCRP-04
    - Rows that produce an empty company name after normalization

    Handles:
    - HTML cells with nested tags (<strong>, <a>, <br>) — per SCRP-03
    - Continuation rows (↳) inheriting company from the most recent
      non-continuation row — per SCRP-05

    Args:
        content: Raw bytes of the README file.
        source_repo: Repository slug (e.g., "Summer2026-Internships").

    Returns:
        List of Job objects with job_id, content_hash, and source_repo
        populated. All jobs have status=ACTIVE.
    """
    text = content.decode("utf-8", errors="replace")

    # Extract all <tr> rows from <tbody> sections
    extractor = _TableRowExtractor()
    extractor.feed(text)

    jobs: list[Job] = []
    last_company: str = ""
    seen_ids: set[str] = set()

    for cells in extractor.rows:
        # Need at least 3 cells: company, role, location
        if len(cells) < 3:
            logger.debug("Skipping row with fewer than 3 cells")
            continue

        company_cell = cells[0]
        role_cell = cells[1]
        location_cell = cells[2]
        link_cell = cells[3] if len(cells) > 3 else ""
        date_cell = cells[4].strip() if len(cells) > 4 else None

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
                company_cell[:80],
            )
            continue

        # Clean role title — strip any remaining HTML-like artifacts and emoji badges
        role_title = _strip_html(role_cell).strip()
        # Remove trailing emoji badges like 🎓
        role_title = re.sub(r"\s*[\U0001F393\U0001F525\U0001F6C2\U0001F1FA\U0001F1F8]+\s*$", "", role_title).strip()

        # Location: already has <br> converted to ", " by the extractor
        location_raw = location_cell.strip().strip(", ")
        # Clean any residual HTML from location
        if "<" in location_raw:
            location_raw = _strip_html(location_raw)

        # Extract apply URL from link cell
        primary_url = _extract_url(link_cell) if link_cell else None

        job_id = generate_job_id(
            company_name=company_name,
            role_title=role_title,
            primary_url=primary_url or "",
        )

        # Deduplicate within this parse run
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

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
