"""Mailgun email notifications for the daily pipeline.

Sends start/completion emails via the Mailgun REST API using httpx.
HTML-formatted for readability. Gracefully skips if MAILGUN_API_KEY is not configured.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
from loguru import logger

from wekruit_matching.config import get_settings

# ---------------------------------------------------------------------------
# Shared styles
# ---------------------------------------------------------------------------

_STYLES = """
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; color: #2d2013; background: #f9f6f1; margin: 0; padding: 0; }
.container { max-width: 640px; margin: 0 auto; padding: 24px; }
.header { background: #3b2f1e; color: #faf5ed; padding: 20px 24px; border-radius: 8px 8px 0 0; }
.header h1 { margin: 0; font-size: 20px; font-weight: 600; }
.header .subtitle { color: #c4b8a5; font-size: 13px; margin-top: 4px; }
.body-card { background: #ffffff; border: 1px solid #e8dfd3; border-top: none; border-radius: 0 0 8px 8px; padding: 24px; }
.stat-row { display: flex; gap: 16px; margin-bottom: 20px; }
.stat-box { flex: 1; background: #faf5ed; border-radius: 8px; padding: 14px 16px; text-align: center; }
.stat-box .number { font-size: 28px; font-weight: 700; color: #3b2f1e; }
.stat-box .label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: #8a7d6b; margin-top: 2px; }
.stat-box.green .number { color: #2d7d46; }
.stat-box.amber .number { color: #c77b1f; }
.stat-box.red .number { color: #c0392b; }
h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.8px; color: #8a7d6b; border-bottom: 1px solid #e8dfd3; padding-bottom: 6px; margin: 24px 0 12px; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; padding: 8px 10px; background: #faf5ed; color: #5c5040; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
td { padding: 8px 10px; border-bottom: 1px solid #f0e9df; }
tr:last-child td { border-bottom: none; }
.tag { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.tag-ok { background: #e8f5e9; color: #2d7d46; }
.tag-err { background: #fdecea; color: #c0392b; }
.stale-company { font-weight: 600; color: #3b2f1e; }
.stale-role { color: #8a7d6b; font-size: 12px; }
.error-box { background: #fdecea; border: 1px solid #f5c6cb; border-radius: 6px; padding: 12px 16px; margin-top: 16px; }
.error-box li { color: #922; font-size: 13px; margin: 4px 0; }
.footer { text-align: center; font-size: 11px; color: #b0a594; margin-top: 20px; }
"""


def _send_email(subject: str, html: str, text: str = "") -> bool:
    """Send an email via Mailgun REST API. Returns True on success."""
    settings = get_settings()
    if not settings.mailgun_api_key:
        logger.debug("Mailgun not configured -- skipping email")
        return False

    try:
        response = httpx.post(
            f"https://api.mailgun.net/v3/{settings.mailgun_domain}/messages",
            auth=("api", settings.mailgun_api_key),
            data={
                "from": f"WeKruit Pipeline <pipeline@{settings.mailgun_domain}>",
                "to": [settings.pipeline_notify_email],
                "subject": subject,
                "html": html,
                "text": text or subject,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        logger.info("Email sent: {}", subject)
        return True
    except Exception as e:
        logger.warning("Failed to send email '{}': {}", subject, e)
        return False


# ---------------------------------------------------------------------------
# Start email
# ---------------------------------------------------------------------------

def send_pipeline_start_email() -> bool:
    """Send a notification that the daily pipeline has started."""
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    html = f"""<!DOCTYPE html><html><head><style>{_STYLES}</style></head><body>
<div class="container">
  <div class="header">
    <h1>Pipeline Started</h1>
    <div class="subtitle">{now}</div>
  </div>
  <div class="body-card">
    <p style="margin:0 0 16px;color:#5c5040;">The daily job pipeline is running:</p>
    <table>
      <tr><td style="width:28px;color:#c77b1f;font-weight:700;">1.</td><td>Scrape SimplifyJobs + JobRight GitHub repos</td></tr>
      <tr><td style="color:#c77b1f;font-weight:700;">2.</td><td>Enrich new jobs (industry, skills, sponsorship)</td></tr>
      <tr><td style="color:#c77b1f;font-weight:700;">3.</td><td>Generate embeddings for semantic matching</td></tr>
    </table>
    <p style="margin:16px 0 0;color:#8a7d6b;font-size:12px;">A completion report will follow.</p>
  </div>
  <div class="footer">WeKruit Matching Engine</div>
</div>
</body></html>"""

    return _send_email(
        subject=f"[WeKruit] Pipeline started -- {now}",
        html=html,
    )


# ---------------------------------------------------------------------------
# Completion email
# ---------------------------------------------------------------------------

def _scrape_rows_html(scrape_stats: dict[str, dict]) -> str:
    """Build HTML table rows for scrape stats."""
    rows = []
    for source, stats in scrape_stats.items():
        if "error" in stats:
            rows.append(
                f'<tr><td>{source}</td><td>--</td><td>--</td><td>--</td>'
                f'<td><span class="tag tag-err">ERROR</span></td></tr>'
                f'<tr><td colspan="5" style="color:#922;font-size:12px;padding-left:20px;">'
                f'{stats["error"]}</td></tr>'
            )
        else:
            ins = stats.get("inserted", 0)
            stale = stats.get("stale", 0)
            unch = stats.get("unchanged", 0)
            rows.append(
                f'<tr><td>{source}</td>'
                f'<td style="text-align:right;font-weight:600;color:#2d7d46;">'
                f'{ins:,}</td>'
                f'<td style="text-align:right;">{unch:,}</td>'
                f'<td style="text-align:right;color:#c77b1f;">{stale:,}</td>'
                f'<td><span class="tag tag-ok">OK</span></td></tr>'
            )
    return "\n".join(rows)


def _stale_html(stale_jobs: list[dict]) -> str:
    """Build HTML section for recently staled jobs."""
    if not stale_jobs:
        return ""

    by_company: dict[str, list[str]] = {}
    for job in stale_jobs:
        company = job.get("company_name", "Unknown")
        role = job.get("role_title", "Unknown Role")
        by_company.setdefault(company, []).append(role)

    rows = []
    for company in sorted(by_company.keys()):
        roles = by_company[company]
        role_list = ", ".join(roles[:3])
        extra = f" +{len(roles) - 3} more" if len(roles) > 3 else ""
        rows.append(
            f'<tr><td><span class="stale-company">{company}</span></td>'
            f'<td><span class="stale-role">{role_list}{extra}</span></td></tr>'
        )

    return f"""
    <h2>Marked Outdated (last 24h)</h2>
    <table>
      <tr><th>Company</th><th>Roles</th></tr>
      {"".join(rows)}
    </table>"""


def send_pipeline_complete_email(
    scrape_stats: dict[str, dict],
    jd_stats: dict[str, int],
    enrich_stats: dict[str, int],
    embed_stats: dict[str, int],
    duration_seconds: float,
    errors: list[str],
    stale_jobs: list[dict] | None = None,
    url_resolution_stats: dict | None = None,
) -> bool:
    """Send an HTML notification with pipeline results."""
    now = datetime.now(timezone.utc).strftime("%b %d, %Y %H:%M UTC")
    duration_min = duration_seconds / 60

    total_inserted = sum(
        s.get("inserted", 0) for s in scrape_stats.values() if "error" not in s
    )
    total_stale = sum(
        s.get("stale", 0) for s in scrape_stats.values() if "error" not in s
    )
    enriched = enrich_stats.get("enriched", 0)
    jd_processed = jd_stats.get("processed", 0)
    jd_failed = jd_stats.get("failed", 0)
    credits_used = jd_stats.get("credits_used", 0)
    failed_by_source = jd_stats.get("failed_by_source", {})
    embedded = embed_stats.get("embedded", 0)
    enrich_failed = enrich_stats.get("failed", 0)
    embed_failed = embed_stats.get("failed", 0)

    url_res = url_resolution_stats or {}
    total_resolved = url_res.get("total_resolved", 0)
    resolution_rate = url_res.get("resolution_rate", 0.0)

    has_errors = bool(errors) or any("error" in s for s in scrape_stats.values())
    status_label = "Completed with Errors" if has_errors else "Completed"
    status_color = "#c0392b" if has_errors else "#2d7d46"

    scrape_rows = _scrape_rows_html(scrape_stats)
    stale_section = _stale_html(stale_jobs or [])

    error_section = ""
    if errors:
        items = "".join(f"<li>{e}</li>" for e in errors)
        error_section = f'<div class="error-box"><strong>Errors</strong><ul style="margin:8px 0 0;padding-left:18px;">{items}</ul></div>'

    jd_failure_rows = ""
    if failed_by_source:
        jd_failure_rows = "".join(
            (
                f"<tr><td>JD failures ({source})</td>"
                f"<td style=\"text-align:right;\">{count:,}</td>"
                f"<td style=\"text-align:right;color:#c0392b;\">source-specific</td></tr>"
            )
            for source, count in sorted(failed_by_source.items())
        )

    html = f"""<!DOCTYPE html><html><head><style>{_STYLES}</style></head><body>
<div class="container">
  <div class="header">
    <h1 style="color:{status_color};">{status_label}</h1>
    <div class="subtitle">{now} &middot; {duration_min:.0f} min</div>
  </div>
  <div class="body-card">

    <!-- Top stats -->
    <div style="display:flex;gap:12px;margin-bottom:20px;">
      <div style="flex:1;background:#e8f5e9;border-radius:8px;padding:14px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#2d7d46;">{total_inserted:,}</div>
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:#5a8a5e;">New Jobs</div>
      </div>
      <div style="flex:1;background:#fff8e1;border-radius:8px;padding:14px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#c77b1f;">{total_stale:,}</div>
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:#a08840;">Stale Removed</div>
      </div>
      <div style="flex:1;background:#e3f2fd;border-radius:8px;padding:14px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#1565c0;">{jd_processed:,}</div>
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:#5085b0;">JD Fetches</div>
      </div>
      <div style="flex:1;background:#f3e5f5;border-radius:8px;padding:14px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#7b1fa2;">{enriched:,}</div>
        <div style="font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:#9060a0;">Metadata</div>
      </div>
    </div>

    <!-- Scraping details -->
    <h2>Scraping</h2>
    <table>
      <tr><th>Source</th><th style="text-align:right;">New</th><th style="text-align:right;">Unchanged</th><th style="text-align:right;">Stale</th><th>Status</th></tr>
      {scrape_rows}
    </table>

    <!-- Enrichment & Embedding -->
    <h2>Processing</h2>
    <table>
      <tr><td>ATS JD enrichment</td><td style="text-align:right;">{jd_processed:,} attempted</td><td style="text-align:right;color:#c0392b;">{jd_failed} failed</td></tr>
      <tr><td>Firecrawl credits</td><td style="text-align:right;">{credits_used:,} used</td><td style="text-align:right;color:#8a7d6b;">ATS fallback only</td></tr>
      {jd_failure_rows}
      <tr><td>URL resolution</td>
          <td style="text-align:right;">{total_resolved:,} resolved</td>
          <td style="text-align:right;color:#8a7d6b;">{resolution_rate:.1%} rate</td></tr>
      <tr><td>Enrichment (LLM metadata)</td><td style="text-align:right;">{enriched:,} classified</td><td style="text-align:right;color:#c0392b;">{enrich_failed} failed</td></tr>
      <tr><td>Embedding (OpenAI)</td><td style="text-align:right;">{embedded:,} vectors</td><td style="text-align:right;color:#c0392b;">{embed_failed} failed</td></tr>
    </table>

    {stale_section}
    {error_section}
  </div>
  <div class="footer">WeKruit Matching Engine &middot; {duration_min:.0f} min runtime</div>
</div>
</body></html>"""

    return _send_email(
        subject=f"[WeKruit] {'+' if total_inserted else ''}{total_inserted:,} new jobs | {total_stale:,} stale -- {now}",
        html=html,
    )
