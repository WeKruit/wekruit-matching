"""Internal jobs browser and operational UI pages.

Mounted under /internal on the matching engine API.
"""
from __future__ import annotations

import html
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from wekruit_matching.db.connection import get_connection

router = APIRouter(prefix="/internal", tags=["internal"])

_PER_PAGE = 50
_MAX_PAGE = 2000  # cap OFFSET scans


def _esc(val: object | None) -> str:
    """Escape for safe HTML interpolation."""
    return html.escape(str(val)) if val not in (None, "") else ""


def _fmt_date(value: object | None) -> str:
    """Format a DB date or timestamp as YYYY-MM-DD."""
    if not value:
        return "Not yet"
    return str(value).replace("T", " ")[:10]


def _fmt_timestamp(value: object | None) -> str:
    """Format a DB date or timestamp as YYYY-MM-DD HH:MM."""
    if not value:
        return "Not yet"
    return str(value).replace("T", " ")[:16]


def _query_url(path: str, **params: object) -> str:
    """Build a URL with proper query-string encoding."""
    filtered = {key: value for key, value in params.items() if value not in ("", None)}
    query = urlencode(filtered)
    return f"{path}?{query}" if query else path


_filter_cache: dict = {"ts": 0, "sources": [], "industries": []}


def _get_filter_options() -> tuple[list[dict], list[dict]]:
    """Return cached source/industry filter options for five minutes."""
    now = time.monotonic()
    if now - _filter_cache["ts"] < 300 and _filter_cache["sources"]:
        return _filter_cache["sources"], _filter_cache["industries"]

    with get_connection() as conn:
        sources = conn.execute(
            "SELECT DISTINCT source_repo FROM jobs ORDER BY source_repo"
        ).fetchall()
        industries = conn.execute(
            "SELECT DISTINCT industry FROM jobs "
            "WHERE industry IS NOT NULL ORDER BY industry"
        ).fetchall()

    _filter_cache["sources"] = sources
    _filter_cache["industries"] = industries
    _filter_cache["ts"] = now
    return sources, industries


_CSS = """
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  --wk-bg:#f6f0e8;
  --wk-surface:#fffdf9;
  --wk-surface-muted:#f3ece3;
  --wk-surface-strong:#2b180a;
  --wk-border:#e3d6c5;
  --wk-border-strong:#cdb79c;
  --wk-text:#2b180a;
  --wk-text-muted:#6d5946;
  --wk-text-soft:#8d7763;
  --wk-accent:#e8923a;
  --wk-accent-soft:#fbebd9;
  --wk-success:#2d7d46;
  --wk-success-soft:#eaf5ed;
  --wk-warning:#9a5c0b;
  --wk-warning-soft:#fbefde;
  --wk-danger:#b24536;
  --wk-danger-soft:#fbe8e4;
  --wk-info:#1f5f94;
  --wk-info-soft:#e8f1f9;
  margin:0;
  background:
    radial-gradient(circle at top right, rgba(232,146,58,.12), transparent 22rem),
    linear-gradient(180deg, #fbf7f2 0%, var(--wk-bg) 100%);
  color:var(--wk-text);
  font-family:"Geist", "Inter", "Segoe UI", sans-serif;
  line-height:1.5;
}
body[data-surface="external"]{
  --wk-bg:#fbf6ef;
  --wk-surface:#fffdfa;
  --wk-surface-muted:#f8f1e8;
}
a{color:inherit}
button,input,select{font:inherit}
.skip-link{
  position:absolute;
  left:16px;
  top:-48px;
  padding:10px 14px;
  border-radius:12px;
  background:var(--wk-surface-strong);
  color:#fff;
  text-decoration:none;
  z-index:30;
}
.skip-link:focus{top:16px}
.sr-only{
  position:absolute;
  width:1px;
  height:1px;
  padding:0;
  margin:-1px;
  overflow:hidden;
  clip:rect(0,0,0,0);
  white-space:nowrap;
  border:0;
}
:focus-visible{
  outline:2px solid var(--wk-accent);
  outline-offset:3px;
}
.shell{
  min-height:100vh;
}
.topbar{
  position:sticky;
  top:0;
  z-index:20;
  backdrop-filter:blur(16px);
  background:rgba(251,247,242,.92);
  border-bottom:1px solid rgba(205,183,156,.55);
}
.topbar-inner{
  max-width:1280px;
  margin:0 auto;
  padding:16px 24px;
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:20px;
}
.brand-lockup{
  display:flex;
  align-items:center;
  gap:12px;
  min-width:0;
}
.brand-mark{
  font-family:"Halant", "Iowan Old Style", "Times New Roman", serif;
  font-size:1.35rem;
  font-weight:600;
  letter-spacing:-.02em;
}
.brand-subtitle{
  color:var(--wk-text-soft);
  font-size:.86rem;
  white-space:nowrap;
}
.surface-pill{
  display:inline-flex;
  align-items:center;
  gap:6px;
  min-height:32px;
  padding:6px 12px;
  border:1px solid var(--wk-border);
  border-radius:999px;
  background:rgba(255,255,255,.72);
  color:var(--wk-text-muted);
  font-size:.75rem;
  font-weight:600;
  letter-spacing:.08em;
  text-transform:uppercase;
}
.surface-pill::before{
  content:"";
  width:8px;
  height:8px;
  border-radius:999px;
  background:var(--wk-accent);
}
.shell-nav{
  display:flex;
  align-items:center;
  gap:10px;
  flex-wrap:wrap;
}
.shell-nav a{
  min-height:44px;
  display:inline-flex;
  align-items:center;
  padding:10px 14px;
  border-radius:999px;
  color:var(--wk-text-muted);
  text-decoration:none;
  font-size:.95rem;
}
.shell-nav a:hover{
  background:rgba(255,255,255,.8);
  color:var(--wk-text);
}
.shell-nav a[aria-current="page"]{
  background:var(--wk-surface-strong);
  color:#fff;
}
.page-shell{
  max-width:1280px;
  margin:0 auto;
  padding:32px 24px 56px;
}
.page-hero{
  margin-bottom:28px;
  padding:28px;
  border:1px solid rgba(205,183,156,.7);
  border-radius:28px;
  background:
    linear-gradient(135deg, rgba(255,255,255,.94), rgba(246,240,232,.94)),
    var(--wk-surface);
  box-shadow:0 18px 40px rgba(52,31,10,.06);
}
.page-kicker{
  margin:0 0 8px;
  color:var(--wk-text-soft);
  font-size:.76rem;
  font-weight:700;
  letter-spacing:.14em;
  text-transform:uppercase;
}
.page-title{
  margin:0;
  font-family:"Halant", "Iowan Old Style", "Times New Roman", serif;
  font-size:clamp(2rem, 4vw, 3.1rem);
  line-height:1.02;
  letter-spacing:-.03em;
}
.page-summary{
  max-width:56rem;
  margin:10px 0 0;
  color:var(--wk-text-muted);
  font-size:1rem;
}
.summary-strip{
  display:grid;
  grid-template-columns:repeat(auto-fit, minmax(180px, 1fr));
  gap:12px;
  margin-top:20px;
}
.summary-item{
  min-height:88px;
  padding:14px 16px;
  border:1px solid var(--wk-border);
  border-radius:20px;
  background:rgba(255,255,255,.76);
}
.summary-item-label{
  display:block;
  margin-bottom:6px;
  color:var(--wk-text-soft);
  font-size:.76rem;
  font-weight:700;
  letter-spacing:.08em;
  text-transform:uppercase;
}
.summary-item-value{
  display:block;
  font-size:1.15rem;
  font-weight:700;
}
.page-stack{
  display:grid;
  gap:22px;
}
.surface-card{
  border:1px solid var(--wk-border);
  border-radius:24px;
  background:rgba(255,253,249,.88);
  box-shadow:0 10px 28px rgba(52,31,10,.04);
}
.section{
  padding:22px;
}
.section-head{
  display:flex;
  align-items:flex-start;
  justify-content:space-between;
  gap:18px;
  margin-bottom:18px;
}
.section-actions{
  display:flex;
  align-items:center;
  justify-content:flex-end;
  gap:12px;
  flex-wrap:wrap;
}
.section-title{
  margin:0;
  font-family:"Halant", "Iowan Old Style", "Times New Roman", serif;
  font-size:1.55rem;
  line-height:1.1;
}
.section-copy{
  margin:6px 0 0;
  color:var(--wk-text-muted);
  max-width:48rem;
}
.filters{
  display:grid;
  grid-template-columns:repeat(4, minmax(0, 1fr));
  gap:14px;
}
.filter-shell{
  display:grid;
  gap:16px;
}
.filter-toolbar{
  display:flex;
  align-items:flex-start;
  justify-content:space-between;
  gap:16px;
  flex-wrap:wrap;
}
.filter-helper{
  margin:0;
  max-width:44rem;
  color:var(--wk-text-muted);
  font-size:.95rem;
}
.filter-field{
  display:flex;
  flex-direction:column;
  gap:8px;
}
.filter-field label{
  color:var(--wk-text-muted);
  font-size:.82rem;
  font-weight:700;
  letter-spacing:.04em;
  text-transform:uppercase;
}
.filter-field input,
.filter-field select{
  width:100%;
  min-height:48px;
  padding:12px 14px;
  border:1px solid var(--wk-border);
  border-radius:14px;
  background:#fff;
  color:var(--wk-text);
}
.filter-actions{
  display:flex;
  align-items:flex-end;
  gap:12px;
  flex-wrap:wrap;
}
.button-primary{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  min-height:48px;
  padding:12px 18px;
  border:0;
  border-radius:14px;
  background:var(--wk-surface-strong);
  color:#fff;
  font-weight:600;
  cursor:pointer;
}
.button-primary:hover{background:#3a2412}
.button-secondary{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  min-height:48px;
  padding:12px 18px;
  border:1px solid var(--wk-border);
  border-radius:14px;
  background:#fff;
  color:var(--wk-text);
  font-weight:600;
  text-decoration:none;
}
.button-secondary:hover{
  background:#fff8ef;
  border-color:var(--wk-border-strong);
}
.filter-chips{
  display:flex;
  gap:10px;
  flex-wrap:wrap;
}
.filter-chip{
  display:inline-flex;
  align-items:center;
  gap:8px;
  min-height:36px;
  padding:8px 12px;
  border-radius:999px;
  border:1px solid var(--wk-border);
  background:rgba(255,255,255,.82);
  color:var(--wk-text-muted);
  font-size:.86rem;
}
.filter-chip strong{
  color:var(--wk-text);
  font-size:.78rem;
  font-weight:700;
  letter-spacing:.06em;
  text-transform:uppercase;
}
.badge-row{
  display:flex;
  gap:8px;
  flex-wrap:wrap;
}
.badge{
  display:inline-flex;
  align-items:center;
  min-height:30px;
  padding:5px 10px;
  border-radius:999px;
  border:1px solid transparent;
  font-size:.78rem;
  font-weight:600;
  line-height:1.2;
}
.badge-neutral{background:var(--wk-surface-muted);color:var(--wk-text-muted);border-color:#e5dacc}
.badge-success{background:var(--wk-success-soft);color:var(--wk-success);border-color:#cde4d3}
.badge-warning{background:var(--wk-warning-soft);color:var(--wk-warning);border-color:#ecd0aa}
.badge-danger{background:var(--wk-danger-soft);color:var(--wk-danger);border-color:#efc6bf}
.badge-info{background:var(--wk-info-soft);color:var(--wk-info);border-color:#c6d8e8}
.stats-grid{
  display:grid;
  grid-template-columns:repeat(auto-fit, minmax(200px, 1fr));
  gap:14px;
}
.metric-card{
  min-height:132px;
  padding:18px;
  border:1px solid var(--wk-border);
  border-radius:22px;
  background:#fff;
}
.metric-card .eyebrow{
  display:block;
  color:var(--wk-text-soft);
  font-size:.78rem;
  font-weight:700;
  letter-spacing:.08em;
  text-transform:uppercase;
}
.metric-card .value{
  display:block;
  margin-top:10px;
  font-size:2rem;
  font-weight:700;
  line-height:1;
}
.metric-card .detail{
  display:block;
  margin-top:12px;
  color:var(--wk-text-muted);
  font-size:.92rem;
}
.metric-card.is-success .value{color:var(--wk-success)}
.metric-card.is-danger .value{color:var(--wk-danger)}
.metric-card.is-info .value{color:var(--wk-info)}
.metric-card.is-warning .value{color:var(--wk-warning)}
.table-wrap{
  overflow-x:auto;
  border:1px solid var(--wk-border);
  border-radius:20px;
  background:#fff;
  contain:layout paint;
  content-visibility:auto;
  contain-intrinsic-size:720px;
}
table{
  width:100%;
  border-collapse:collapse;
  min-width:780px;
}
caption{
  padding:14px 16px 0;
  text-align:left;
  color:var(--wk-text-muted);
}
th{
  padding:14px 16px;
  background:var(--wk-surface-muted);
  color:var(--wk-text-muted);
  text-align:left;
  font-size:.78rem;
  font-weight:700;
  letter-spacing:.08em;
  text-transform:uppercase;
  white-space:nowrap;
}
td{
  padding:16px;
  border-top:1px solid #f0e7dc;
  vertical-align:top;
  font-size:.95rem;
}
tbody tr:hover td{background:#fffaf3}
.job-title-link{
  color:var(--wk-text);
  font-weight:700;
  text-decoration:none;
}
.job-title-link:hover{color:var(--wk-info)}
.job-meta{
  margin-top:8px;
  color:var(--wk-text-soft);
  font-size:.84rem;
}
.muted{
  color:var(--wk-text-muted);
}
.soft{
  color:var(--wk-text-soft);
}
.truncate{
  max-width:220px;
  overflow:hidden;
  text-overflow:ellipsis;
  white-space:nowrap;
}
.skills-cell{
  max-width:220px;
}
.status-stack{
  display:flex;
  flex-wrap:wrap;
  gap:8px;
}
.job-list-mobile{
  display:none;
  list-style:none;
  margin:0;
  padding:0;
  content-visibility:auto;
  contain-intrinsic-size:960px;
}
.job-card{
  padding:18px;
  border:1px solid var(--wk-border);
  border-radius:22px;
  background:#fff;
}
.job-card + .job-card{
  margin-top:14px;
}
.job-card-top{
  display:flex;
  align-items:flex-start;
  justify-content:space-between;
  gap:12px;
}
.job-card-title{
  margin:0;
  font-size:1rem;
}
.job-card-company{
  margin:6px 0 0;
  color:var(--wk-text-muted);
}
.job-card-grid{
  display:grid;
  grid-template-columns:repeat(2, minmax(0, 1fr));
  gap:12px;
  margin-top:16px;
}
.job-card-block dt{
  color:var(--wk-text-soft);
  font-size:.78rem;
  font-weight:700;
  letter-spacing:.08em;
  text-transform:uppercase;
}
.job-card-block dd{
  margin:6px 0 0;
  color:var(--wk-text);
}
.job-card-skills{
  margin-top:14px;
}
.results-toolbar{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:16px;
  flex-wrap:wrap;
  margin-bottom:18px;
}
.results-meta{
  margin:0;
  color:var(--wk-text-muted);
  font-size:.95rem;
}
.pagination{
  display:flex;
  justify-content:center;
  align-items:center;
  gap:8px;
  flex-wrap:wrap;
}
.pagination-link,
.pagination-current{
  min-height:44px;
  min-width:44px;
  padding:10px 14px;
  border-radius:14px;
  border:1px solid var(--wk-border);
  display:inline-flex;
  align-items:center;
  justify-content:center;
  text-decoration:none;
  background:#fff;
}
.pagination-link:hover{background:#fff8ef}
.pagination-current{
  background:var(--wk-surface-strong);
  border-color:var(--wk-surface-strong);
  color:#fff;
  font-weight:700;
}
.pagination-link.is-disabled{
  background:var(--wk-surface-muted);
  color:var(--wk-text-soft);
  cursor:not-allowed;
}
.pagination.pagination-compact{
  justify-content:flex-start;
  gap:6px;
}
.pagination.pagination-compact .pagination-link,
.pagination.pagination-compact .pagination-current{
  min-width:40px;
  min-height:40px;
  padding:8px 12px;
  border-radius:12px;
}
.key-value{
  display:grid;
  gap:12px;
  content-visibility:auto;
  contain-intrinsic-size:520px;
}
.key-value-row{
  display:grid;
  grid-template-columns:180px 1fr;
  gap:16px;
  padding:14px 0;
  border-top:1px solid #eee2d5;
}
.key-value-row:first-child{
  padding-top:0;
  border-top:0;
}
.key-value-row dt{
  color:var(--wk-text-muted);
  font-weight:700;
}
.key-value-row dd{
  margin:0;
}
.note{
  padding:16px 18px;
  border:1px solid #ecd7b8;
  border-radius:18px;
  background:#fff7ea;
  color:var(--wk-text-muted);
}
.empty{
  padding:28px;
  border:1px dashed var(--wk-border-strong);
  border-radius:22px;
  background:rgba(255,255,255,.7);
  color:var(--wk-text-muted);
  text-align:center;
}
code{
  padding:2px 6px;
  border-radius:8px;
  background:#efe4d7;
  font-family:"SFMono-Regular", "Consolas", monospace;
  font-size:.85em;
}
@media (max-width:1080px){
  .filters{
    grid-template-columns:repeat(2, minmax(0, 1fr));
  }
}
@media (max-width:900px){
  .topbar-inner{
    align-items:flex-start;
    flex-direction:column;
  }
  .page-shell{
    padding:24px 16px 48px;
  }
  .page-hero,
  .section{
    padding:20px;
  }
  .desktop-only{
    display:none;
  }
  .job-list-mobile{
    display:block;
  }
  .table-wrap.mobile-hidden{
    display:none;
  }
}
@media (max-width:720px){
  .summary-strip,
  .stats-grid,
  .filters,
  .job-card-grid{
    grid-template-columns:1fr;
  }
  .filter-toolbar,
  .results-toolbar,
  .section-actions{
    align-items:stretch;
    flex-direction:column;
  }
  .filter-actions{
    align-items:stretch;
  }
  .button-primary{
    width:100%;
  }
  .button-secondary{
    width:100%;
  }
  .section-head,
  .job-card-top{
    flex-direction:column;
  }
  .key-value-row{
    grid-template-columns:1fr;
    gap:6px;
  }
  .surface-pill,
  .brand-subtitle{
    white-space:normal;
  }
}
"""


def _badge(label: str, tone: str = "neutral") -> str:
    """Render a reusable badge with tone classes."""
    return f'<span class="badge badge-{tone}">{_esc(label)}</span>'


def _summary_item(label: str, value: str) -> str:
    """Render a page summary tile."""
    return (
        '<div class="summary-item">'
        f'<span class="summary-item-label">{_esc(label)}</span>'
        f'<span class="summary-item-value">{_esc(value)}</span>'
        "</div>"
    )


def _section(title: str, copy: str, body: str, actions: str = "") -> str:
    """Render a shared surface-card section."""
    actions_html = f'<div class="section-actions">{actions}</div>' if actions else ""
    return f"""
    <section class="surface-card section">
      <div class="section-head">
        <div>
          <h2 class="section-title">{_esc(title)}</h2>
          <p class="section-copy">{_esc(copy)}</p>
        </div>
        {actions_html}
      </div>
      {body}
    </section>"""


def _metric_card(label: str, value: str, detail: str, tone: str) -> str:
    """Render a shared stats card."""
    return f"""
    <article class="metric-card is-{tone}">
      <span class="eyebrow">{_esc(label)}</span>
      <span class="value">{_esc(value)}</span>
      <span class="detail">{_esc(detail)}</span>
    </article>"""


def _job_state_badges(row: dict) -> str:
    """Render text-based badges for sponsorship and processing state."""
    if row["sponsorship"] is True:
        sponsorship = _badge("Sponsorship supported", "success")
    elif row["sponsorship"] is False:
        sponsorship = _badge("No sponsorship", "danger")
    else:
        sponsorship = _badge("Sponsorship unknown", "neutral")

    status = _badge(
        "Active listing" if row["status"] == "active" else "Inactive listing",
        "success" if row["status"] == "active" else "danger",
    )

    enriched = _badge(
        "Enriched" if row["enriched_at"] else "Pending enrichment",
        "info" if row["enriched_at"] else "warning",
    )

    if row["embedded_at"]:
        embedded = _badge("Embedded", "success")
    elif row["enriched_at"]:
        embedded = _badge("Pending embedding", "warning")
    else:
        embedded = _badge("Awaiting enrichment", "neutral")

    return f'<div class="status-stack">{status}{sponsorship}{enriched}{embedded}</div>'


def _filter_chip(label: str, value: str) -> str:
    """Render a compact active-filter chip."""
    return (
        '<span class="filter-chip">'
        f"<strong>{_esc(label)}</strong>"
        f"<span>{_esc(value)}</span>"
        "</span>"
    )


def _pagination_nav(
    *,
    page: int,
    total_pages: int,
    page_url,
    aria_label: str,
    compact: bool = False,
) -> str:
    """Render pagination links with proper disabled boundary states."""
    pagination_parts: list[str] = []
    class_name = "pagination pagination-compact" if compact else "pagination"

    if page <= 1:
        pagination_parts.append(
            '<span class="pagination-link is-disabled" aria-disabled="true">Prev</span>'
        )
    else:
        pagination_parts.append(
            f'<a class="pagination-link" href="{_esc(page_url(page - 1))}" '
            'aria-label="Previous page">Prev</a>'
        )

    start_page = max(1, page - 3)
    end_page = min(total_pages, start_page + 6)
    start_page = max(1, end_page - 6)
    for number in range(start_page, end_page + 1):
        if number == page:
            pagination_parts.append(
                f'<span class="pagination-current" aria-current="page">{number}</span>'
            )
        else:
            pagination_parts.append(
                f'<a class="pagination-link" href="{_esc(page_url(number))}" '
                f'aria-label="Page {number}">{number}</a>'
            )

    if page >= total_pages:
        pagination_parts.append(
            '<span class="pagination-link is-disabled" aria-disabled="true">Next</span>'
        )
    else:
        pagination_parts.append(
            f'<a class="pagination-link" href="{_esc(page_url(page + 1))}" '
            'aria-label="Next page">Next</a>'
        )

    return (
        f'<nav class="{class_name}" aria-label="{_esc(aria_label)}">'
        f'{"".join(pagination_parts)}</nav>'
    )


def _page_shell(
    *,
    title: str,
    nav_active: str,
    intro_html: str,
    content_html: str,
    surface: str = "internal",
) -> str:
    """Wrap content in the shared WeKruit page shell."""
    nav_items = [
        ("jobs", "Active Jobs", "/internal/jobs?status=active"),
        ("stale", "Stale Jobs", "/internal/jobs?status=inactive"),
        ("stats", "Stats", "/internal/stats"),
        ("pipeline", "Pipeline", "/internal/pipeline"),
    ]
    nav_html = "".join(
        (
            f'<a href="{href}"'
            f' {"aria-current=\"page\"" if name == nav_active else ""}>{label}</a>'
        )
        for name, label, href in nav_items
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{_esc(title)} · WeKruit Matching</title>
  <style>{_CSS}</style>
</head>
<body data-surface="{_esc(surface)}">
  <a class="skip-link" href="#main-content">Skip to content</a>
  <div class="shell">
    <header class="topbar">
      <div class="topbar-inner">
        <div class="brand-lockup">
          <div>
            <div class="brand-mark">WeKruit Matching</div>
            <div class="brand-subtitle">
              Jobs console for inventory, freshness, and pipeline health
            </div>
          </div>
          <span class="surface-pill">{surface}</span>
        </div>
        <nav class="shell-nav" aria-label="Primary navigation">
          {nav_html}
        </nav>
      </div>
    </header>
    <main id="main-content" class="page-shell">
      {intro_html}
      <div class="page-stack">
        {content_html}
      </div>
    </main>
  </div>
</body>
</html>"""


@router.get("/jobs", response_class=HTMLResponse)
def jobs_browser(
    page: int = Query(1, ge=1, le=_MAX_PAGE),
    status: str = Query("active"),
    source: str = Query(""),
    industry: str = Query(""),
    q: str = Query("", max_length=100),
):
    """Paginated jobs table with responsive browsing and shared shell."""
    if status not in ("active", "inactive"):
        status = "active"

    where_clauses = ["status = %(status)s"]
    params: dict[str, object] = {"status": status, "limit": _PER_PAGE, "offset": 0}

    if source:
        where_clauses.append("source_repo = %(source)s")
        params["source"] = source
    if industry:
        where_clauses.append("industry = %(industry)s")
        params["industry"] = industry
    if q:
        where_clauses.append("(company_name ILIKE %(q)s OR role_title ILIKE %(q)s)")
        params["q"] = f"%{q}%"

    where = " AND ".join(where_clauses)

    with get_connection() as conn:
        count_row = conn.execute(
            f"SELECT COUNT(*) AS total FROM jobs WHERE {where}",
            params,
        ).fetchone()
        total = count_row["total"] if count_row else 0
        total_pages = max(1, (total + _PER_PAGE - 1) // _PER_PAGE)
        page = min(page, total_pages) if total else 1
        offset = (page - 1) * _PER_PAGE
        params["offset"] = offset

        rows = conn.execute(
            f"""
            SELECT job_id, source_repo, company_name, role_title, primary_url,
                   location_raw, industry, company_size, required_skills, sponsorship,
                   status, first_seen_at, last_seen_at, enriched_at, embedded_at
            FROM jobs
            WHERE {where}
            ORDER BY first_seen_at DESC
            LIMIT %(limit)s OFFSET %(offset)s
            """,
            params,
        ).fetchall()

    sources, industries_list = _get_filter_options()
    result_start = offset + 1 if total else 0
    result_end = min(offset + _PER_PAGE, total) if total else 0
    active_filters = sum(1 for value in (source, industry, q) if value)
    clear_filters_url = _query_url("/internal/jobs", status=status)

    source_opts = "".join(
        (
            f'<option value="{_esc(row["source_repo"])}"'
            f' {"selected" if row["source_repo"] == source else ""}>'
            f'{_esc(row["source_repo"])}</option>'
        )
        for row in sources
    )
    industry_opts = "".join(
        (
            f'<option value="{_esc(row["industry"])}"'
            f' {"selected" if row["industry"] == industry else ""}>'
            f'{_esc(row["industry"])}</option>'
        )
        for row in industries_list
    )
    active_filter_chips = "".join(
        chip
        for chip in (
            _filter_chip("Search", q) if q else "",
            _filter_chip("Source", source) if source else "",
            _filter_chip("Industry", industry) if industry else "",
        )
        if chip
    )

    page_title = "Active Jobs" if status == "active" else "Stale Jobs"
    page_summary = (
        "Browse the live inventory, narrow to a source or industry, and see processing "
        "state without decoding raw database fields."
        if status == "active"
        else "Review listings that have dropped out of the active corpus and confirm what "
        "went stale, where it came from, and how far processing got."
    )

    intro_html = f"""
    <header class="page-hero">
      <p class="page-kicker">Jobs Console</p>
      <h1 class="page-title">{page_title}</h1>
      <p class="page-summary">{page_summary}</p>
      <div class="summary-strip" aria-label="Page summary">
        {_summary_item("Inventory", "Active" if status == "active" else "Inactive")}
        {_summary_item("Results", f"{total:,} jobs")}
        {_summary_item("Showing", f"{result_start}-{result_end}" if total else "0")}
        {_summary_item("Refinements", str(active_filters))}
      </div>
    </header>"""

    filter_feedback = (
        f"""
        <div class="filter-toolbar">
          <p class="filter-helper">
            Showing a narrowed view of the inventory. Remove any refinement to widen the list.
          </p>
          <a class="button-secondary" href="{_esc(clear_filters_url)}">Clear refinements</a>
        </div>
        <div class="filter-chips" aria-label="Active filters">
          {active_filter_chips}
        </div>"""
        if active_filters
        else """
        <p class="filter-helper">
          Start broad, then narrow by source, industry, or a role/company search
          when you need a tighter slice.
        </p>"""
    )

    filter_body = f"""
    <div class="filter-shell">
      {filter_feedback}
      <form class="filters" method="get" action="/internal/jobs" role="search">
        <div class="filter-field">
          <label for="q">Search</label>
          <input
            id="q"
            type="text"
            name="q"
            placeholder="Company or role"
            value="{_esc(q)}"
          >
        </div>
        <div class="filter-field">
          <label for="status-select">Listing status</label>
          <select id="status-select" name="status">
            <option value="active" {"selected" if status == "active" else ""}>Active</option>
            <option value="inactive" {"selected" if status == "inactive" else ""}>Inactive</option>
          </select>
        </div>
        <div class="filter-field">
          <label for="source-select">Source</label>
          <select id="source-select" name="source">
            <option value="">All sources</option>
            {source_opts}
          </select>
        </div>
        <div class="filter-field">
          <label for="industry-select">Industry</label>
          <select id="industry-select" name="industry">
            <option value="">All industries</option>
            {industry_opts}
          </select>
        </div>
        <div class="filter-actions">
          <button class="button-primary" type="submit">Apply filters</button>
        </div>
      </form>
    </div>"""

    filters_html = _section(
        "Browse and filter",
        "Keep one filtering surface for search, source, industry, and listing "
        "status, with a visible read on what is currently narrowing the list.",
        filter_body,
    )

    def page_url(target_page: int) -> str:
        return _query_url(
            "/internal/jobs",
            page=target_page,
            status=status,
            source=source,
            industry=industry,
            q=q,
        )

    if not rows:
        empty_action = (
            f'<p><a class="button-secondary" href="{_esc(clear_filters_url)}">'
            "Clear refinements</a></p>"
            if active_filters
            else ""
        )
        jobs_body = f"""
        <div class="empty">
          <p>No jobs matched the current filters.</p>
          <p class="soft">Try widening source, industry, or text search.</p>
          {empty_action}
        </div>"""
    else:
        desktop_rows: list[str] = []
        mobile_rows: list[str] = []

        for row in rows:
            role_title = _esc(row["role_title"]) or "Untitled role"
            company_name = _esc(row["company_name"]) or "Unknown company"
            location = _esc(row["location_raw"]) or "Location not provided"
            industry_badge = (
                _badge(row["industry"], "info")
                if row["industry"] and row["industry"] != "unknown"
                else _badge("Industry unknown", "neutral")
            )
            skills = row["required_skills"] or []
            skills_html = "".join(_badge(skill, "neutral") for skill in skills[:5]) or _badge(
                "No skills parsed",
                "neutral",
            )
            if len(skills) > 5:
                skills_html += _badge(f"+{len(skills) - 5} more", "neutral")

            row_badges = _job_state_badges(row)
            detail_meta = (
                f"First seen {_fmt_date(row['first_seen_at'])} · "
                f"Last seen {_fmt_date(row['last_seen_at'])}"
            )

            desktop_rows.append(
                f"""
                <tr>
                  <td>
                    <a
                      class="job-title-link"
                      href="{_esc(row['primary_url'])}"
                      target="_blank"
                      rel="noopener"
                    >
                      {role_title}
                    </a>
                    <div class="job-meta">{_esc(detail_meta)}</div>
                  </td>
                  <td class="truncate" title="{company_name}">{company_name}</td>
                  <td>{location}</td>
                  <td>{industry_badge}</td>
                  <td class="skills-cell"><div class="badge-row">{skills_html}</div></td>
                  <td>{row_badges}</td>
                </tr>"""
            )

            mobile_rows.append(
                f"""
                <li class="job-card">
                  <div class="job-card-top">
                    <div>
                      <h3 class="job-card-title">
                        <a
                          class="job-title-link"
                          href="{_esc(row['primary_url'])}"
                          target="_blank"
                          rel="noopener"
                        >
                          {role_title}
                        </a>
                      </h3>
                      <p class="job-card-company">{company_name}</p>
                    </div>
                    {industry_badge}
                  </div>
                  <dl class="job-card-grid">
                    <div class="job-card-block">
                      <dt>Location</dt>
                      <dd>{location}</dd>
                    </div>
                    <div class="job-card-block">
                      <dt>Freshness</dt>
                      <dd>{_esc(detail_meta)}</dd>
                    </div>
                  </dl>
                  <div class="job-card-skills">
                    <div class="badge-row">{skills_html}</div>
                  </div>
                  <div class="job-card-skills">
                    {row_badges}
                  </div>
                </li>"""
            )

        results_toolbar = f"""
        <div class="results-toolbar">
          <p class="results-meta">
            Showing {result_start:,}-{result_end:,} of {total:,} jobs · Page {page} of {total_pages}
          </p>
          {_pagination_nav(
              page=page,
              total_pages=total_pages,
              page_url=page_url,
              aria_label="Pagination",
              compact=True,
          ) if total_pages > 1 else ""}
        </div>"""

        jobs_body = f"""
        {results_toolbar}
        <div class="table-wrap mobile-hidden">
          <table>
            <caption class="sr-only">{page_title} results</caption>
            <thead>
              <tr>
                <th>Role</th>
                <th>Company</th>
                <th>Location</th>
                <th>Industry</th>
                <th>Skills</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>{''.join(desktop_rows)}</tbody>
          </table>
        </div>
        <ul class="job-list-mobile" aria-label="{_esc(page_title)} mobile results">
          {''.join(mobile_rows)}
        </ul>"""

    jobs_section = _section(
        "Job inventory",
        "Desktop keeps density with a table. Narrow screens switch to cards so core fields "
        "stay readable without horizontal-only browsing.",
        jobs_body,
    )

    pagination_html = ""
    if total_pages > 1:
        pagination_html = _section(
            "Result pages",
            "Paging keeps your current filters, clamps invalid page numbers, and "
            "uses real disabled states at the boundaries.",
            _pagination_nav(
                page=page,
                total_pages=total_pages,
                page_url=page_url,
                aria_label="Pagination",
            ),
        )

    content_html = f"{filters_html}{jobs_section}{pagination_html}"
    nav_active = "stale" if status == "inactive" else "jobs"
    return _page_shell(
        title=page_title,
        nav_active=nav_active,
        intro_html=intro_html,
        content_html=content_html,
    )


@router.get("/stats", response_class=HTMLResponse)
def stats_dashboard():
    """Inventory overview with a shared summary hierarchy."""
    with get_connection() as conn:
        summary = conn.execute(
            """
            SELECT
              COUNT(*) AS total,
              COUNT(*) FILTER (WHERE status = 'active') AS active,
              COUNT(*) FILTER (WHERE status = 'inactive') AS inactive,
              COUNT(*) FILTER (
                WHERE enriched_at IS NOT NULL AND status = 'active'
              ) AS enriched,
              COUNT(*) FILTER (
                WHERE embedded_at IS NOT NULL AND status = 'active'
              ) AS embedded
            FROM jobs
            """
        ).fetchone()

        source_rows = conn.execute(
            """
            SELECT source_repo, status, COUNT(*) AS count
            FROM jobs
            GROUP BY source_repo, status
            ORDER BY source_repo, status
            """
        ).fetchall()

        industry_rows = conn.execute(
            """
            SELECT industry, COUNT(*) AS count
            FROM jobs
            WHERE status = 'active' AND industry IS NOT NULL AND industry != 'unknown'
            GROUP BY industry
            ORDER BY count DESC
            LIMIT 15
            """
        ).fetchall()

        recent_rows = conn.execute(
            """
            SELECT DATE(first_seen_at) AS day, COUNT(*) AS count
            FROM jobs
            WHERE first_seen_at > NOW() - INTERVAL '14 days'
            GROUP BY DATE(first_seen_at)
            ORDER BY day DESC
            """
        ).fetchall()

    total = summary["total"] if summary else 0
    active = summary["active"] if summary else 0
    inactive = summary["inactive"] if summary else 0
    enriched = summary["enriched"] if summary else 0
    embedded = summary["embedded"] if summary else 0

    intro_html = f"""
    <header class="page-hero">
      <p class="page-kicker">Inventory Health</p>
      <h1 class="page-title">Stats</h1>
      <p class="page-summary">
        Start with the size and freshness of the corpus, then drill into source mix,
        industry coverage, and recent intake without switching visual patterns.
      </p>
      <div class="summary-strip" aria-label="Stats summary">
        {_summary_item("Total jobs", f"{total:,}")}
        {_summary_item("Active", f"{active:,}")}
        {_summary_item("Inactive", f"{inactive:,}")}
        {_summary_item("Processed", f"{embedded:,} embedded")}
      </div>
    </header>"""

    overview_cards = "".join(
        [
            _metric_card(
                "Active jobs",
                f"{active:,}",
                "Listings currently visible in the live corpus",
                "success",
            ),
            _metric_card(
                "Inactive jobs",
                f"{inactive:,}",
                "Listings that dropped out of active inventory",
                "danger",
            ),
            _metric_card(
                "Enriched",
                f"{enriched:,}",
                "Active jobs with metadata classification complete",
                "info",
            ),
            _metric_card(
                "Embedded",
                f"{embedded:,}",
                "Active jobs ready for vector-based matching",
                "warning",
            ),
        ]
    )
    overview_body = f"""
    <div class="stats-grid">
      {overview_cards}
    </div>"""

    source_map: dict[str, dict[str, int]] = {}
    for row in source_rows:
        source_map.setdefault(row["source_repo"], {})[row["status"]] = row["count"]

    source_table = [
        "<div class=\"table-wrap\"><table>",
        "<caption class=\"sr-only\">Source mix</caption>",
        "<thead><tr><th>Source</th><th>Active</th><th>Inactive</th></tr></thead><tbody>",
    ]
    for source_name, counts in sorted(source_map.items()):
        source_table.append(
            "<tr>"
            f"<td>{_esc(source_name)}</td>"
            f"<td>{counts.get('active', 0):,}</td>"
            f"<td>{counts.get('inactive', 0):,}</td>"
            "</tr>"
        )
    source_table.append("</tbody></table></div>")

    industry_table = [
        "<div class=\"table-wrap\"><table>",
        "<caption class=\"sr-only\">Top industries</caption>",
        "<thead><tr><th>Industry</th><th>Active jobs</th></tr></thead><tbody>",
    ]
    for row in industry_rows:
        industry_table.append(
            "<tr>"
            f"<td>{_badge(row['industry'], 'info')}</td>"
            f"<td>{row['count']:,}</td>"
            "</tr>"
        )
    industry_table.append("</tbody></table></div>")

    recent_table = [
        "<div class=\"table-wrap\"><table>",
        "<caption class=\"sr-only\">Recent intake</caption>",
        "<thead><tr><th>Date</th><th>New jobs</th></tr></thead><tbody>",
    ]
    for row in recent_rows:
        recent_table.append(
            "<tr>"
            f"<td>{_esc(row['day'])}</td>"
            f"<td>{row['count']:,}</td>"
            "</tr>"
        )
    recent_table.append("</tbody></table></div>")

    content_html = "".join(
        [
            _section(
                "Inventory at a glance",
                "Lead with the counts that tell you how much of the corpus is live "
                "and how much is ready for downstream use.",
                overview_body,
            ),
            _section(
                "Source mix",
                "Compare active and inactive inventory by upstream source to catch "
                "imbalance quickly.",
                "".join(source_table),
            ),
            _section(
                "Top industries",
                "See which categories dominate the active inventory right now.",
                "".join(industry_table),
            ),
            _section(
                "Recent intake",
                "Track how many new jobs entered the corpus during the last two weeks.",
                "".join(recent_table),
            ),
        ]
    )

    return _page_shell(
        title="Stats",
        nav_active="stats",
        intro_html=intro_html,
        content_html=content_html,
    )


@router.get("/pipeline", response_class=HTMLResponse)
def pipeline_status():
    """Pipeline health page with future customer-facing copy."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE status = 'active'
                  AND (job_description IS NULL OR job_description = '')
                  AND primary_url IS NOT NULL
                  AND primary_url NOT LIKE 'https://jobright.ai/%'
                  AND jd_fetch_attempted_at IS NULL
              ) AS pending_jd_queue,
              COUNT(*) FILTER (
                WHERE jd_fetch_source = 'failed'
              ) AS failed_fetches,
              COUNT(*) FILTER (
                WHERE embedded_at IS NULL
                  AND enriched_at IS NOT NULL
                  AND status = 'active'
              ) AS pending_embed,
              MAX(last_seen_at) AS last_scrape,
              MAX(enriched_at) AS last_enriched,
              MAX(embedded_at) AS last_embedded
            FROM jobs
            """
        ).fetchone()

        source_rows = conn.execute(
            """
            SELECT
              COALESCE(jd_fetch_source, 'null') AS source,
              COUNT(*) FILTER (
                WHERE job_description IS NOT NULL AND job_description != ''
              ) AS with_jd,
              COUNT(*) FILTER (
                WHERE job_description IS NULL OR job_description = ''
              ) AS without_jd
            FROM jobs
            WHERE status = 'active'
            GROUP BY COALESCE(jd_fetch_source, 'null')
            ORDER BY source
            """
        ).fetchall()

        quality = conn.execute(
            """
            SELECT
              COUNT(*) FILTER (
                WHERE data_quality_score < 50
              ) AS below_50,
              COUNT(*) FILTER (
                WHERE data_quality_score >= 50 AND data_quality_score < 80
              ) AS between_50_79,
              COUNT(*) FILTER (
                WHERE data_quality_score >= 80
              ) AS at_least_80,
              COUNT(*) FILTER (
                WHERE data_quality_score IS NULL
              ) AS not_scored
            FROM jobs
            WHERE status = 'active'
            """
        ).fetchone()

    pending_jd_queue = row["pending_jd_queue"] if row else 0
    failed_fetches = row["failed_fetches"] if row else 0
    pending_embed = row["pending_embed"] if row else 0
    last_scrape = _fmt_timestamp(row["last_scrape"]) if row else "Not yet"
    last_enriched = _fmt_timestamp(row["last_enriched"]) if row else "Not yet"
    last_embedded = _fmt_timestamp(row["last_embedded"]) if row else "Not yet"
    quality = quality or {}

    intro_html = f"""
    <header class="page-hero">
      <p class="page-kicker">Pipeline Health</p>
      <h1 class="page-title">Pipeline</h1>
      <p class="page-summary">
        Show JD coverage, queue depth, quality, and stage freshness in product language
        instead of raw operational shorthand.
      </p>
      <div class="summary-strip" aria-label="Pipeline summary">
        {_summary_item("Waiting for JD fetch", f"{pending_jd_queue:,}")}
        {_summary_item("Failed attempts", f"{failed_fetches:,}")}
        {_summary_item("Waiting for embedding", f"{pending_embed:,}")}
        {_summary_item("Last embed", last_embedded)}
      </div>
    </header>"""

    backlog_body = f"""
    <div class="stats-grid">
      {_metric_card(
          "Jobs waiting for JD fetch",
          f"{pending_jd_queue:,}",
          "Active listings still missing a fetched description and not yet attempted.",
          "success" if pending_jd_queue == 0 else "warning",
      )}
      {_metric_card(
          "Failed JD attempts",
          f"{failed_fetches:,}",
          "Listings already attempted once but still missing fetched job-description content.",
          "danger" if failed_fetches else "success",
      )}
      {_metric_card(
          "Jobs waiting for embeddings",
          f"{pending_embed:,}",
          "Listings that already have metadata but are not ready for semantic matching yet.",
          "success" if pending_embed == 0 else "warning",
      )}
    </div>"""

    coverage_table = [
        "<div class=\"table-wrap\"><table>",
        "<caption class=\"sr-only\">JD coverage by source</caption>",
        "<thead><tr><th>Source</th><th>With JD</th><th>Without JD</th></tr></thead><tbody>",
    ]
    for source_row in source_rows:
        coverage_table.append(
            "<tr>"
            f"<td>{_badge(source_row['source'], 'info')}</td>"
            f"<td>{source_row['with_jd']:,}</td>"
            f"<td>{source_row['without_jd']:,}</td>"
            "</tr>"
        )
    coverage_table.append("</tbody></table></div>")

    quality_body = f"""
    <div class="stats-grid">
      {_metric_card(
          "Below 50",
          f"{quality.get('below_50', 0):,}",
          "Low-confidence descriptions that still need operator attention.",
          "danger" if quality.get("below_50", 0) else "success",
      )}
      {_metric_card(
          "50-79",
          f"{quality.get('between_50_79', 0):,}",
          "Usable descriptions with some missing structure or missing salary detail.",
          "warning",
      )}
      {_metric_card(
          "80+",
          f"{quality.get('at_least_80', 0):,}",
          "High-quality descriptions ready for downstream use.",
          "success",
      )}
      {_metric_card(
          "Not scored",
          f"{quality.get('not_scored', 0):,}",
          "Active jobs that still have no JD quality score recorded.",
          "info",
      )}
    </div>"""

    activity_body = f"""
    <dl class="key-value">
      <div class="key-value-row">
        <dt>Scrape refresh</dt>
        <dd>
          {_esc(last_scrape)}
          <div class="soft">Latest time the source inventory was refreshed.</div>
        </dd>
      </div>
      <div class="key-value-row">
        <dt>Metadata enrichment</dt>
        <dd>
          {_esc(last_enriched)}
          <div class="soft">Latest time job classification and enrichment completed.</div>
        </dd>
      </div>
      <div class="key-value-row">
        <dt>Embedding generation</dt>
        <dd>
          {_esc(last_embedded)}
          <div class="soft">Latest time semantic vectors were written for matching.</div>
        </dd>
      </div>
    </dl>"""

    note_body = (
        "<div class=\"note\">"
        "The daily pipeline currently runs once each morning. Operational logs live at "
        "<code>/tmp/matching-daily-update.log</code>."
        "</div>"
    )

    content_html = "".join(
        [
            _section(
                "Processing backlog",
                "Lead with the queues that matter so people can tell immediately "
                "whether the system is keeping up.",
                backlog_body,
            ),
            _section(
                "JD coverage by source",
                "Separate successful JD coverage from remaining gaps by fetch source so "
                "operators can see where the pipeline is strong or weak.",
                "".join(coverage_table),
            ),
            _section(
                "Quality distribution",
                "Bucket JD quality scores so low-confidence fetches surface immediately.",
                quality_body,
            ),
            _section(
                "Recent pipeline activity",
                "Translate internal timestamps into stage names that still make sense "
                "in a later customer-facing surface.",
                activity_body,
            ),
            _section(
                "Operational note",
                "Keep the unavoidable implementation detail isolated instead of making "
                "it the page headline.",
                note_body,
            ),
        ]
    )

    return _page_shell(
        title="Pipeline",
        nav_active="pipeline",
        intro_html=intro_html,
        content_html=content_html,
    )
