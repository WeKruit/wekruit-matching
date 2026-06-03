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

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Iterable

import httpx
from loguru import logger

from wekruit_matching.models.job import Job
from wekruit_matching.scraper.id_utils import compute_content_hash, generate_job_id

# Loguru placeholders are `{}`, not `{}` / `{}`. See `pipeline/daily.py`.

# ---------------------------------------------------------------------------
# Tunables (single place — keep in sync with `.planning/INITIATIVE-vc-
# portfolio-job-boards.md`).
# ---------------------------------------------------------------------------

#: Default Firecrawl wait — most Getro / Consider SPAs settle in 6s on
#: warm playwright workers. Override per board when slow.
DEFAULT_WAIT_MS = 6000

#: Render-completeness retry (2026-06-03). A single fixed-waitFor render often
#: returns thin/partial markdown for JS-heavy boards (the SPA had not finished
#: hydrating), under-capturing jobs — the mark_stale circuit-breaker then trips
#: ("partial render, skipped deactivation") and we miss that board's new jobs.
#: scrape_board now retries with a longer wait when a render looks incomplete
#: (markdown shorter than MIN_RENDER_MARKDOWN_CHARS OR zero parsed jobs) and
#: keeps the best (most-jobs) attempt. A fully-rendered board returns on the
#: first attempt, so good boards pay no extra latency.
RENDER_ATTEMPT_WAIT_MULTIPLIERS: tuple[float, ...] = (1.0, 2.0)
MIN_RENDER_MARKDOWN_CHARS = 500

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
    VCBoardConfig("nea",        "https://careers.nea.com/jobs",             "NEA",                 wait_ms=8000),
    VCBoardConfig("lightspeed", "https://jobs.lsvp.com/jobs",               "Lightspeed",          wait_ms=8000),
    VCBoardConfig("bessemer",   "https://jobs.bvp.com/jobs",                "Bessemer",            wait_ms=8000),
    VCBoardConfig("battery",    "https://jobs.battery.com/jobs",            "Battery Ventures",    wait_ms=8000),
    VCBoardConfig("contrary",   "https://jobs.contrary.com/jobs",           "Contrary",            wait_ms=8000),

    # ---- Getro-backed (5 funds) ----
    VCBoardConfig("accel",      "https://jobs.accel.com/jobs",              "Accel",               wait_ms=5000),
    VCBoardConfig("khosla",     "https://jobs.khoslaventures.com/jobs",     "Khosla Ventures",     wait_ms=5000),
    VCBoardConfig("gc",         "https://jobs.generalcatalyst.com/jobs",    "General Catalyst",   wait_ms=5000),
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

        # Use the canonical id_utils generator so we get a 64-char SHA-256
        # that fits `jobs.job_id character varying(64)`. The cross-source
        # collision policy is intentional: if Stripe SWE appears on both
        # vcboard:a16z and vcboard:sequoia, the differing source_repo
        # produces two distinct ids — we keep both rows and let downstream
        # canonical-signature dedup (Phase B follow-up) collapse them.
        job_id = generate_job_id(
            source_repo=f"vcboard:{board.slug}",
            company_name=company_name,
            role_title=title,
        )

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
                content_hash=compute_content_hash(company_name, title),
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

    if out:
        return out

    # ---- Fallback: Consider layout (a16z / Sequoia / KP / Greylock /
    # Lightspeed / Bessemer / Battery / NEA / Contrary). These boards
    # don't use the `#### [...](url)` heading shape — they emit flat
    # `[Title](ats_url)` links directly to Greenhouse/Lever/Ashby +
    # `[Company](portfoliojobs.<fund>.com/jobs/<slug>)` preceding it.
    # See `.planning/INITIATIVE-vc-portfolio-job-boards.md` § layout cheat.
    out_consider = _parse_consider_flat_links(markdown, board, now=now, max_jobs=max_jobs)
    if out_consider:
        return out_consider

    # ---- 3rd fallback: Ashby layout (Pear VC). Format:
    #
    #     CompanyName
    #     ------
    #
    #     [### Job Title - CompanyName\
    #     \
    #     CompanyName • <location> • <type>](https://jobs.ashbyhq.com/<org>/<uuid>)
    out_ashby = _parse_ashby_jobs(markdown, board, now=now, max_jobs=max_jobs)
    if out_ashby:
        return out_ashby

    logger.warning(
        "vc_board.parse: 0 jobs from {} ({} headings, {} chars) — "
        "board layout may have changed",
        board.slug,
        len(headings),
        len(markdown),
    )
    return out


# Ashby renders job listings as: `[### Title - Company\\\n\\\nLine](url)`. The
# anchor text has the `### ` heading marker baked INSIDE the link, then a
# title, " - ", company, a line break, and a one-line metadata blurb. URL
# is `jobs.ashbyhq.com/<org-slug>/<uuid>` (UUID v4).
_ASHBY_JOB_RE = re.compile(
    r"\[###\s+(?P<title>[^\\\n\]]+?)\s+-\s+(?P<company>[^\\\n\]]+?)\\?\s*\n"
    r"(?:\\?\s*\n)?"
    r"(?P<meta>[^\]]+?)\]\((?P<url>https?://jobs\.ashbyhq\.com/[^)]+)\)",
    re.MULTILINE,
)


def _parse_ashby_jobs(
    markdown: str,
    board: VCBoardConfig,
    *,
    now: datetime,
    max_jobs: int,
) -> list[Job]:
    """Fallback parser for Ashby-hosted VC portfolio job boards (Pear etc.)."""
    out: list[Job] = []
    for m in _ASHBY_JOB_RE.finditer(markdown):
        if len(out) >= max_jobs:
            break
        title = m.group("title").strip()
        company = m.group("company").strip()
        url = m.group("url").strip()
        if not (title and company and url):
            continue
        # Stage hint sits in `meta` ("• Full time • Remote" — no stage info
        # there usually, so fall back to board-level default).
        meta = m.group("meta") or ""
        stage = _infer_stage(meta) or board.default_company_stage
        job_id = generate_job_id(
            source_repo=f"vcboard:{board.slug}",
            company_name=company,
            role_title=title,
        )
        out.append(
            Job(
                job_id=job_id,
                source_repo=f"vcboard:{board.slug}",
                company_name=company,
                role_title=title,
                primary_url=url,
                ats_apply_url=url,
                location_raw="",
                date_posted_raw=None,
                first_seen_at=now,
                last_seen_at=now,
                content_hash=compute_content_hash(company, title),
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
    if out:
        logger.info(
            "vc_board.parse[ashby]: {} jobs from {} ({} chars)",
            len(out), board.slug, len(markdown),
        )
    return out


# ATS-link sniffer for the Consider-layout fallback. Matches the canonical
# `(text)(url)` link form where the URL is a known external ATS host. Limited
# to the hosts we've actually observed on Consider boards so a misleading
# `[Apply](https://blog.<fund>.com/x)` link doesn't get treated as a job.
_CONSIDER_ATS_LINK_RE = re.compile(
    r"\[(?P<title>[^\]]{2,200})\]\((?P<url>https?://(?:"
    r"job-boards\.greenhouse\.io|boards\.greenhouse\.io|"
    r"jobs\.lever\.co|jobs\.ashbyhq\.com|"
    r"apply\.workable\.com|jobs\.smartrecruiters\.com|"
    r"[a-z0-9-]+\.recruitee\.com|[a-z0-9-]+\.workable\.com|"
    r"[a-z0-9-]+\.bamboohr\.com|[a-z0-9-]+\.teamtailor\.com"
    r")/[^)]+)\)"
)

# Company-link shape Consider emits right above each ATS link.
#
# Critical: REQUIRES the host to be on a non-ATS domain. Both Consider's
# company-filter URLs and the ATS job URLs share the `/jobs/<slug>` path
# shape, so we discriminate by host. Without this, the regex was matching
# `[Senior Data Engineer](https://job-boards.greenhouse.io/.../jobs/12345)`
# (a job title link) AS a company anchor, which then attached the next
# real job to the wrong "company name".
_CONSIDER_ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "smartrecruiters.com",
    "recruitee.com",
    "bamboohr.com",
    "teamtailor.com",
)
_CONSIDER_COMPANY_LINK_RE = re.compile(
    # Host capture is mandatory so we can reject ATS links. The optional
    # `(?:[^)]*?/)?` segment handles boards where the company filter is at
    # `/jobs/<slug>` directly (a16z) OR nested like `/x/jobs/<slug>` (some
    # Consider variants). Slug allows hyphens but not digits-only (rules
    # out job IDs that ATS systems use).
    r"\[(?P<name>[^\]]{1,80})\]\((?P<url>https?://(?P<host>[^/]+)/(?:[^)]*?/)?jobs/(?P<slug>[a-z][a-z0-9-]*))\)"
)


def _parse_consider_flat_links(
    markdown: str,
    board: VCBoardConfig,
    *,
    now: datetime,
    max_jobs: int,
) -> list[Job]:
    """Fallback parser for boards using the Consider SaaS layout.

    Walks every ATS-host link in order and pairs it with the most recent
    preceding company link in the same markdown. Skips bare "Apply" links
    so we don't write twice per posting. Pure function.
    """
    out: list[Job] = []
    # First pass: index every company link by its end-offset so we can
    # look up "most recent company before offset X" in O(log n). Reject
    # links whose host is a known ATS — those are job-title links, not
    # company filters, and using them as anchors mis-attributes the next
    # real job to a title string (e.g. "Senior Data Engineer" got logged
    # as a company name on the 2026-05-27 first-pass smoke).
    company_anchors: list[tuple[int, str, str]] = []  # (end_pos, name, slug)
    for cm in _CONSIDER_COMPANY_LINK_RE.finditer(markdown):
        name = cm.group("name").strip()
        host = cm.group("host").lower()
        if any(ats in host for ats in _CONSIDER_ATS_HOSTS):
            continue  # title link to an ATS, not a company filter
        # Consider renders BOTH `[Mercury](.../jobs/mercury)` AND
        # `[All jobs at Mercury](.../jobs/mercury)` for the same company.
        # The "All jobs at " variant was leaking through as a "company"
        # name and getting attached to nearby ATS-link jobs. Strip the
        # known UI prefixes so both forms collapse to the same anchor.
        for prefix in ("all jobs at ", "all openings at ", "jobs at ", "view jobs at "):
            if name.lower().startswith(prefix):
                name = name[len(prefix):].strip()
                break
        if not name or name.lower() in {"apply", "view", "view job", "view all", "all jobs"}:
            continue
        # Filter obviously-not-company strings: long titles (company names
        # are usually < 40 chars), trailing punctuation typical of UI text.
        if len(name) > 50:
            continue
        company_anchors.append((cm.end(), name, cm.group("slug")))

    seen_urls: set[str] = set()
    for m in _CONSIDER_ATS_LINK_RE.finditer(markdown):
        if len(out) >= max_jobs:
            break
        title = m.group("title").strip()
        url = m.group("url").strip()
        # Skip the "Apply" duplicate link Consider emits below every title.
        if title.lower() == "apply" or len(title) < 3:
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        # Find closest preceding company-link end-position.
        offset = m.start()
        company_name: str | None = None
        company_slug: str | None = None
        for end_pos, name, slug in reversed(company_anchors):
            if end_pos < offset:
                company_name = name
                company_slug = slug
                break
        if not company_name:
            continue

        # Look in a small window for stage hints.
        window_end = min(len(markdown), m.end() + 400)
        window = markdown[max(0, m.start() - 200):window_end]
        stage = _infer_stage(window) or board.default_company_stage

        job_id = generate_job_id(
            source_repo=f"vcboard:{board.slug}",
            company_name=company_name,
            role_title=title,
        )
        out.append(
            Job(
                job_id=job_id,
                source_repo=f"vcboard:{board.slug}",
                company_name=company_name,
                role_title=title,
                primary_url=url,
                ats_apply_url=url,
                location_raw="",
                date_posted_raw=None,
                first_seen_at=now,
                last_seen_at=now,
                content_hash=compute_content_hash(company_name, title),
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

    if out:
        logger.info(
            "vc_board.parse[consider]: {} jobs from {} ({} chars)",
            len(out), board.slug, len(markdown),
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
            logger.warning("firecrawl /v1/scrape non-200: {} {}", resp.status_code, resp.text[:200])
            return ""
        try:
            payload = resp.json()
        except ValueError:
            logger.warning("firecrawl returned non-JSON: {}", resp.text[:200])
            return ""
        if not payload.get("success"):
            logger.warning("firecrawl returned success=false: {}", str(payload)[:200])
            return ""
        return payload.get("data", {}).get("markdown", "") or ""


# ---------------------------------------------------------------------------
# Public entry: scrape one board / scrape all boards.
# ---------------------------------------------------------------------------


def scrape_board(client: FirecrawlClient, board: VCBoardConfig) -> list[Job]:
    """Render + parse one board, retrying with a longer wait on a thin/partial
    render. Failure is silent (best-effort list + log).

    A render is "thin" when the markdown is shorter than
    MIN_RENDER_MARKDOWN_CHARS (the SPA almost certainly did not finish loading)
    OR zero jobs parsed. On a thin render we retry at the next wait multiplier;
    we always return the best (most-jobs) attempt so a partial-but-non-empty
    render is never discarded.
    """
    best: list[Job] = []
    n_attempts = len(RENDER_ATTEMPT_WAIT_MULTIPLIERS)
    for attempt, mult in enumerate(RENDER_ATTEMPT_WAIT_MULTIPLIERS, start=1):
        wait_ms = int(board.wait_ms * mult)
        try:
            markdown = client.scrape_markdown(board.url, wait_ms=wait_ms)
        except (httpx.RequestError, httpx.TimeoutException) as e:
            logger.error("firecrawl fetch failed for {} (attempt {}/{}): {}",
                         board.slug, attempt, n_attempts, e)
            markdown = ""
        jobs = parse_markdown_jobs(markdown, board) if markdown else []
        if len(jobs) > len(best):
            best = jobs
        rendered_ok = len(markdown) >= MIN_RENDER_MARKDOWN_CHARS and len(jobs) > 0
        if rendered_ok:
            if attempt > 1:
                logger.info("vc_board.scrape: {} recovered on attempt {}/{} (wait={}ms) → {} jobs",
                            board.slug, attempt, n_attempts, wait_ms, len(jobs))
            else:
                logger.info("vc_board.scrape: {} → {} jobs", board.slug, len(jobs))
            return jobs
        logger.warning(
            "vc_board.scrape: {} thin render attempt {}/{} (md={}c jobs={}) — {}",
            board.slug, attempt, n_attempts, len(markdown), len(jobs),
            "retrying with a longer wait" if attempt < n_attempts else "giving up",
        )
    logger.warning("vc_board.scrape: {} still thin after {} attempt(s) → {} jobs (best-effort)",
                   board.slug, n_attempts, len(best))
    return best


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
