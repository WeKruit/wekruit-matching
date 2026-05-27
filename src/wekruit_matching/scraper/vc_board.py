"""VC portfolio job-board scraper (single adapter, 17+ boards via config).

Why this exists
---------------
Adam ask 2026-05-27: scrape the 17 VC portfolio job boards from his LinkedIn
post (a16z / Sequoia / KP / Greylock / Accel / NEA / Lightspeed / Bessemer /
Battery / Khosla / GC / Index / Contrary / Pear / Antler / BITKRAFT) using
**our existing Firecrawl flow**, not by reverse-engineering Consider / Getro /
Ashby SaaS APIs:

  > "你能直接去每一个这个link去研究一下怎么用我们现成的flow吗？？
  >  为什么要用第三方？？我们都有firecrawl了"

Architecture (one path, no per-platform branching)
--------------------------------------------------
  1. Per board: `firecrawl POST /v1/scrape` with `formats=["markdown"]` and
     `waitFor=<board.wait_ms>`. Self-hosted Firecrawl at
     `FIRECRAWL_BASE_URL` (Adam's laptop → http://host.docker.internal:3002
     when called from the pipeline container; http://localhost:3002 outside).
  2. Parse the rendered markdown with a single deterministic regex pass that
     finds the universal `#### [<Title>](.../jobs/<id>-<slug>)` heading shape
     emitted by Getro / Consider / Ashby / Index / Antler / BITKRAFT (Pear,
     YC, Index all expose a similar shape). When a board prints something
     different, the parser returns an empty list and the next board is
     scraped — no crash.
  3. Each parsed listing becomes a `Job` model instance with
     `source_repo = "vcboard:<board.slug>"` so cross-source dedup can fold
     a Stripe SWE that appears on three VC boards into one canonical doc.
  4. Inferred `company_stage` (D17 canonical token) is best-effort from the
     same markdown — a regex grabs nearby `Series A / Seed / pre-seed`
     mentions; an LLM fallback can plug in later if the regex misses.

Scope guardrails
----------------
- Pure functions + injected deps so unit tests don't need a live Firecrawl.
- No retries here — `daily.py`'s `_stage_timeout` block owns retry policy.
- No DB writes here — caller (Stage 1.7 in `pipeline/daily.py`) hands the
  `Job` list to `upsert.upsert_jobs` which already de-dupes against PG.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

import httpx

from wekruit_matching.models.job import Job

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tunables (single place — keep in sync with `.planning/INITIATIVE-vc-
# portfolio-job-boards.md`).
# ---------------------------------------------------------------------------

#: Default Firecrawl wait — most Getro / Consider SPAs settle in 6s on
#: warm playwright workers. Override per board when slow.
DEFAULT_WAIT_MS = 6000

#: Per-call HTTP timeout (Firecrawl render + extract). 75s gives Sequoia
#: room without blocking the daily pipeline.
DEFAULT_TIMEOUT_S = 75.0

#: Cap how many jobs we accept from one board per run. Protects against a
#: runaway markdown response from melting PG.
MAX_JOBS_PER_BOARD = 5000


# ---------------------------------------------------------------------------
# Per-board config.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VCBoardConfig:
    """One row per board. Add new boards by appending to ``VC_BOARDS``."""

    slug: str  # → `source_repo = "vcboard:<slug>"`
    url: str  # Canonical jobs-listing URL (post-resolve).
    fund_name: str  # Human-readable.
    wait_ms: int = DEFAULT_WAIT_MS

    #: Source-level company-stage hint (used when per-job parse misses).
    #: BITKRAFT = all seed, Antler = mostly pre_seed, etc.
    default_company_stage: str | None = None

    #: Source-level industry hint (BITKRAFT = gaming, etc.).
    default_industry: str | None = None


# Resolved URLs come from `.planning/research/vc-jobs-tier{1,2}.md` +
# `vc-jobs-niche.md`. lnkd.in shortlinks already expanded.
VC_BOARDS: list[VCBoardConfig] = [
    # ---- Consider-backed (9 funds) ----
    VCBoardConfig("a16z",       "https://portfoliojobs.a16z.com/jobs",     "Andreessen Horowitz", wait_ms=8000),
    VCBoardConfig("sequoia",    "https://jobs.sequoiacap.com/jobs",         "Sequoia Capital",     wait_ms=8000),
    VCBoardConfig("kp",         "https://jobs.kleinerperkins.com/jobs",     "Kleiner Perkins",     wait_ms=8000),
    VCBoardConfig("greylock",   "https://jobs.greylock.com/jobs",           "Greylock Partners",   wait_ms=8000),
    VCBoardConfig("nea",        "https://careers.nea.com/job",              "NEA",                 wait_ms=8000),
    VCBoardConfig("lightspeed", "https://jobs.lsvp.com/jobs",               "Lightspeed",          wait_ms=8000),
    VCBoardConfig("bessemer",   "https://jobs.bvp.com/jobs",                "Bessemer",            wait_ms=8000),
    VCBoardConfig("battery",    "https://jobs.battery.com/jobs",            "Battery Ventures",    wait_ms=8000),
    VCBoardConfig("contrary",   "https://jobs.contrary.com/jobs",           "Contrary",            wait_ms=8000),

    # ---- Getro-backed (5 funds) ----
    VCBoardConfig("accel",      "https://jobs.accel.com/jobs",              "Accel",               wait_ms=5000),
    VCBoardConfig("khosla",     "https://jobs.khoslaventures.com/jobs",     "Khosla Ventures",     wait_ms=5000),
    VCBoardConfig("gc",         "https://jobs.generalcatalyst.com/companies","General Catalyst",   wait_ms=5000),
    VCBoardConfig("antler",     "https://careers.antler.co/jobs",           "Antler",              wait_ms=5000,
                  default_company_stage="pre_seed"),
    VCBoardConfig("bitkraft",   "https://careers.bitkraft.vc/jobs",         "BITKRAFT",            wait_ms=5000,
                  default_company_stage="seed",
                  default_industry="gaming_and_esports"),

    # ---- Ashby (1 fund — Pear) ----
    VCBoardConfig("pear",       "https://jobs.ashbyhq.com/pear-vc",         "Pear VC",             wait_ms=5000,
                  default_company_stage="pre_seed"),

    # ---- Custom (1 fund — Index Ventures' Wagtail+ES) ----
    VCBoardConfig("indexvc",    "https://www.indexventures.com/startup-jobs","Index Ventures",     wait_ms=5000),

    # YC handled by existing `yc.py` — do not duplicate here.
]


# ---------------------------------------------------------------------------
# Markdown parser — universal across Getro / Consider / Ashby renders.
# ---------------------------------------------------------------------------

#: Universal job-heading line shape. All three platforms render H4-link.
#:
#:   #### [<TITLE>](<URL>#content)
#:
#: Captures (title, url). URL contains `/companies/<company-slug>/jobs/<id>`.
_JOB_HEADING_RE = re.compile(
    r"^####\s*\[(?P<title>[^\]]+)\]\((?P<url>https?://[^)]+)\)\s*$",
    re.MULTILINE,
)

#: Company-name back-reference (printed right after the heading on most
#: boards). Same `(NAME)(URL#content)` shape but at H1 level → just text.
_COMPANY_LINK_RE = re.compile(
    r"\[(?P<name>[^\]]{1,80})\]\((?P<url>https?://[^)]+/companies/[^)]+)\)",
)

#: Company-slug extractor from the company link URL.
_COMPANY_SLUG_RE = re.compile(r"/companies/(?P<slug>[a-z0-9][a-z0-9-]*)")

#: Stage tokens we'll see in the markdown next to the listing. Mapped to the
#: D17 canonical vocab.
_STAGE_TEXT_TO_CANONICAL: dict[str, str] = {
    "pre-seed":      "pre_seed",
    "pre seed":      "pre_seed",
    "preseed":       "pre_seed",
    "seed":          "seed",
    "series a":      "series_a",
    "series-a":      "series_a",
    "series a1":     "series_a",
    "series a2":     "series_a",
    "series b":      "series_b",
    "series-b":      "series_b",
    "series c":      "series_c",
    "series-c":      "series_c",
    "series d":      "series_d_plus",
    "series e":      "series_d_plus",
    "series f":      "series_d_plus",
    "series g":      "series_d_plus",
    "growth":        "growth",
    "growth stage":  "growth",
    "late stage":    "growth",
    "public":        "ipo",
    "ipo":           "ipo",
    "acquired":      "acquired",
}

_STAGE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in _STAGE_TEXT_TO_CANONICAL) + r")\b",
    re.IGNORECASE,
)


def _infer_stage(text_window: str) -> str | None:
    """Pick the canonical stage token from a chunk of markdown around a job."""
    match = _STAGE_PATTERN.search(text_window)
    if not match:
        return None
    return _STAGE_TEXT_TO_CANONICAL.get(match.group(1).lower())


def _job_id_from_url(url: str) -> str:
    """Extract a stable per-job id from the listing URL.

    Getro: `/companies/{slug}/jobs/{id}-{slug}#content`
    Consider: `/companies/{slug}/jobs/{id}` or `/jobs/{id}-{slug}`
    Ashby: `/companies/{slug}/{id}` (no `/jobs/` segment)

    Strategy: grab the last path segment up to `#` and strip a leading id
    so we get a slug we can content-hash with company/title for dedup.
    """
    # Trim fragment + trailing slash.
    path = url.split("#", 1)[0].rstrip("/")
    last = path.rsplit("/", 1)[-1] or "unknown"
    return last[:200]


def _slugify_company(company_name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-")
    return s or "unknown"


def parse_markdown_jobs(
    markdown: str,
    board: VCBoardConfig,
    *,
    now: datetime | None = None,
    max_jobs: int = MAX_JOBS_PER_BOARD,
) -> list[Job]:
    """Convert Firecrawl-rendered board markdown into a list of `Job` rows.

    Pure function. No HTTP. Easy to fixture in tests.
    """
    now = now or datetime.now(timezone.utc)

    # Walk every H4 link heading. For each, look at ±400 chars around it to
    # find company + stage hints. The window is small enough that two
    # listings can't easily contaminate each other, big enough to catch the
    # company link + stage text that immediately follow on Getro/Consider.
    out: list[Job] = []
    headings = list(_JOB_HEADING_RE.finditer(markdown))
    for h in headings[:max_jobs]:
        title = h.group("title").strip()
        url = h.group("url").strip()
        # Filter out non-job links (e.g., "All jobs" nav). A real listing
        # URL always contains `/jobs/` after `/companies/<slug>/`.
        if "/jobs/" not in url and "ashbyhq.com" not in url:
            continue

        # Window starts at heading, extends 600 chars (covers metadata block
        # printed just below the heading on every board sampled).
        window_end = min(len(markdown), h.end() + 600)
        window = markdown[h.start():window_end]

        # Company name — look for the FIRST company-link in the window
        # whose slug appears in the heading's URL too. Falls back to the
        # first link if no match (handles boards where slug case differs).
        company_name: str | None = None
        company_slug: str | None = None
        slug_from_url = _COMPANY_SLUG_RE.search(url)
        target_slug = slug_from_url.group("slug") if slug_from_url else None
        for cm in _COMPANY_LINK_RE.finditer(window):
            cm_url = cm.group("url")
            # Skip the heading's own URL — that link's anchor text is the
            # job title, not the company name. The heading regex already
            # gave us the title; we only care about the *company* link,
            # which on every sampled board is a sibling URL whose path
            # ends at `/companies/<slug>` (no `/jobs/` segment).
            if "/jobs/" in cm_url:
                continue
            cm_slug = _COMPANY_SLUG_RE.search(cm_url)
            if cm_slug:
                if target_slug and cm_slug.group("slug") != target_slug:
                    continue
                company_name = cm.group("name").strip()
                company_slug = cm_slug.group("slug")
                break

        if not company_name and target_slug:
            # Fall back to humanizing the slug from the URL ("100ms" stays
            # as-is; "fluid-truck" becomes "fluid-truck").
            company_name = target_slug
            company_slug = target_slug
        if not company_name:
            # No way to attribute — skip this row rather than write a
            # company-less job into `matching-jobs`.
            continue

        stage = _infer_stage(window) or board.default_company_stage

        job_id = f"vcboard-{board.slug}-{company_slug}-{_job_id_from_url(url)}"

        out.append(
            Job(
                job_id=job_id,
                source_repo=f"vcboard:{board.slug}",
                company_name=company_name,
                role_title=title,
                primary_url=url,
                ats_apply_url=url,  # Board surface IS the apply URL on every platform sampled.
                location_raw="",  # Filled by Stage 2b enrichment (Firecrawl on the job page).
                date_posted_raw=None,
                first_seen_at=now,
                last_seen_at=now,
                content_hash=None,  # Computed downstream by upsert.
                industry=board.default_industry,
                company_size=None,
                required_skills=[],
                sponsorship=None,
                seniority_level=None,
                role_function=[],
                sources=[f"vcboard:{board.slug}"],
                job_description=None,
            )
        )

    if not out:
        logger.warning(
            "vc_board.parse: 0 jobs from %s (%d headings, %d chars) — "
            "board layout may have changed",
            board.slug,
            len(headings),
            len(markdown),
        )
    return out


# ---------------------------------------------------------------------------
# Firecrawl client (thin httpx wrapper).
# ---------------------------------------------------------------------------


@dataclass
class FirecrawlClient:
    """Minimal `/v1/scrape` caller. Honors `FIRECRAWL_BASE_URL`."""

    base_url: str
    api_key: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    http_post: Callable[..., httpx.Response] | None = None

    def scrape_markdown(self, url: str, wait_ms: int) -> str:
        """POST `/v1/scrape` with `formats=["markdown"]`. Return markdown."""
        headers: dict[str, str] = {"Content-Type": "application/json"}
        # Self-hosted defaults USE_DB_AUTHENTICATION=false → no auth header
        # needed. Cloud Firecrawl wants Bearer; honor either.
        if self.api_key and self.api_key not in ("", "self-hosted-no-auth"):
            headers["Authorization"] = f"Bearer {self.api_key}"

        body = {
            "url": url,
            "formats": ["markdown"],
            "waitFor": wait_ms,
            "timeout": int(self.timeout_s * 1000),
        }
        post = self.http_post or httpx.post
        resp = post(
            f"{self.base_url.rstrip('/')}/v1/scrape",
            headers=headers,
            json=body,
            timeout=self.timeout_s,
        )
        if resp.status_code != 200:
            logger.warning("firecrawl /v1/scrape non-200: %d %s", resp.status_code, resp.text[:200])
            return ""
        try:
            payload = resp.json()
        except ValueError:
            logger.warning("firecrawl returned non-JSON: %s", resp.text[:200])
            return ""
        if not payload.get("success"):
            logger.warning("firecrawl returned success=false: %s", str(payload)[:200])
            return ""
        return payload.get("data", {}).get("markdown", "") or ""


# ---------------------------------------------------------------------------
# Public entry: scrape one board / scrape all boards.
# ---------------------------------------------------------------------------


def scrape_board(client: FirecrawlClient, board: VCBoardConfig) -> list[Job]:
    """Render + parse one board. Failure is silent (empty list + log)."""
    try:
        markdown = client.scrape_markdown(board.url, wait_ms=board.wait_ms)
    except (httpx.RequestError, httpx.TimeoutException) as e:
        logger.error("firecrawl fetch failed for %s: %s", board.slug, e)
        return []
    if not markdown:
        return []
    jobs = parse_markdown_jobs(markdown, board)
    logger.info("vc_board.scrape: %s → %d jobs", board.slug, len(jobs))
    return jobs


def scrape_all_boards(
    client: FirecrawlClient,
    *,
    boards: Iterable[VCBoardConfig] | None = None,
) -> dict[str, list[Job]]:
    """Sequential scrape (Firecrawl handles its own browser-pool concurrency).

    Returns a dict slug → jobs so the pipeline can persist + log per-board.
    Sequential keeps us friendly to a 5-browser local Firecrawl pool.
    """
    boards = boards or VC_BOARDS
    out: dict[str, list[Job]] = {}
    for b in boards:
        out[b.slug] = scrape_board(client, b)
    return out
