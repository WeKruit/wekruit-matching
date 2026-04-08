# Pipeline Reliability & Email Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the daily job scraping/enrichment/embedding pipeline self-healing and observable by fixing 3 bugs (industry vocab, stale-marking timeout, log routing) and adding Mailgun email notifications on pipeline start and completion.

**Architecture:** A new `notifications/` module provides Mailgun email via httpx (already a dependency). A new `pipeline/daily.py` unified orchestrator replaces the fragmented shell script + launchd inline commands, running all stages in sequence with error capture and sending start/completion emails to admin1@wekruit.com. The launchd plist is updated to call this single orchestrator.

**Tech Stack:** Python 3.12, httpx (Mailgun REST API), pydantic-settings (config), psycopg3, loguru, launchd

---

## File Structure

| File | Action | Responsibility |
|------|--------|---------------|
| `src/wekruit_matching/config.py` | Modify | Add optional Mailgun settings |
| `src/wekruit_matching/enrichment/classifier.py` | Modify | Expand industry vocabulary + update LLM prompt |
| `src/wekruit_matching/scraper/upsert.py` | Modify | Batch stale-marking for large ID sets |
| `src/wekruit_matching/notifications/__init__.py` | Create | Package init |
| `src/wekruit_matching/notifications/email.py` | Create | Mailgun email sender via httpx |
| `src/wekruit_matching/pipeline/__init__.py` | Create | Package init |
| `src/wekruit_matching/pipeline/daily.py` | Create | Unified daily orchestrator with email hooks |
| `scripts/daily-update.sh` | Modify | Simplify to call unified orchestrator |
| `~/Library/LaunchAgents/com.wekruit.daily-update.plist` | Modify | Use shell script, merge stderr+stdout |
| `tests/test_industry_vocab.py` | Create | Validate expanded vocab + LLM prompt sync |
| `tests/test_stale_batching.py` | Create | Validate batched stale marking |
| `tests/test_email_notification.py` | Create | Validate email formatting |

---

### Task 1: Expand Industry Vocabulary

**Files:**
- Modify: `src/wekruit_matching/enrichment/classifier.py:33-37` (INDUSTRY_VOCAB)
- Modify: `src/wekruit_matching/enrichment/classifier.py:111-127` (_SYSTEM_PROMPT)
- Create: `tests/test_industry_vocab.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_industry_vocab.py`:

```python
"""Verify industry vocabulary covers real-world sectors and LLM prompt stays in sync."""
from wekruit_matching.enrichment.classifier import INDUSTRY_VOCAB, _SYSTEM_PROMPT


def test_industry_vocab_includes_non_tech_sectors():
    """Industries that previously caused validation failures must be in vocab."""
    required = {
        "telecom", "automotive", "aerospace_defense", "construction",
        "defense", "manufacturing", "retail", "media", "education",
        "government", "energy", "transportation", "hospitality",
        "real_estate", "nonprofit", "legal", "pharma",
    }
    missing = required - INDUSTRY_VOCAB
    assert not missing, f"Missing industries: {missing}"


def test_system_prompt_lists_all_industries():
    """The LLM prompt must list every vocab entry so Haiku picks valid values."""
    for industry in INDUSTRY_VOCAB:
        assert industry in _SYSTEM_PROMPT, (
            f"Industry '{industry}' is in INDUSTRY_VOCAB but missing from _SYSTEM_PROMPT"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && .venv/bin/python -m pytest tests/test_industry_vocab.py -v`

Expected: FAIL — missing industries + prompt out of sync

- [ ] **Step 3: Expand INDUSTRY_VOCAB and update _SYSTEM_PROMPT**

In `src/wekruit_matching/enrichment/classifier.py`, replace lines 33-37:

```python
INDUSTRY_VOCAB: frozenset[str] = frozenset({
    "tech", "fintech", "healthtech", "ecommerce", "enterprise_saas",
    "ai_ml", "cybersecurity", "gaming", "social_media", "hardware",
    "consulting", "telecom", "automotive", "aerospace_defense",
    "construction", "defense", "manufacturing", "retail", "media",
    "education", "government", "energy", "transportation",
    "hospitality", "real_estate", "nonprofit", "legal", "pharma",
    "other", "unknown",
})
```

And replace the `_SYSTEM_PROMPT` industry line (line ~116) to list all values:

```python
_SYSTEM_PROMPT = """\
You are a job-listing classifier. Given a job listing, return ONLY a JSON object
with these exact keys — no explanation, no markdown, no extra text:

{
  "industry": "<one of: tech, fintech, healthtech, ecommerce, enterprise_saas, ai_ml, cybersecurity, gaming, social_media, hardware, consulting, telecom, automotive, aerospace_defense, construction, defense, manufacturing, retail, media, education, government, energy, transportation, hospitality, real_estate, nonprofit, legal, pharma, other, unknown>",
  "company_size": "<one of: startup, midsize, large, unknown>",
  "skills_inferred": ["<skill1>", "<skill2>", ...],
  "likely_sponsors_visa": <true | false | null>
}

Rules:
- Use "unknown" when there is insufficient signal — never guess.
- For likely_sponsors_visa: true = explicitly offers, false = explicitly does not, null = no signal.
- skills_inferred should list common technical skills implied by the role title and company type.
- Output valid JSON only. No markdown code fences.
"""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && .venv/bin/python -m pytest tests/test_industry_vocab.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_industry_vocab.py src/wekruit_matching/enrichment/classifier.py
git commit -m "fix: expand industry vocabulary to cover non-tech sectors"
```

---

### Task 2: Fix Stale-Marking Timeout for Large Repos

**Files:**
- Modify: `src/wekruit_matching/scraper/upsert.py:124-161` (mark_stale_jobs)
- Create: `tests/test_stale_batching.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_stale_batching.py`:

```python
"""Verify mark_stale_jobs handles large ID sets by batching."""
from unittest.mock import MagicMock, call
from wekruit_matching.scraper.upsert import mark_stale_jobs, _STALE_BATCH_SIZE


def test_mark_stale_batches_large_id_sets():
    """When seen_ids exceeds batch size, query runs in chunks."""
    # Create fake IDs exceeding batch size
    seen_ids = {f"job_{i}" for i in range(_STALE_BATCH_SIZE + 100)}

    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.rowcount = 5
    mock_conn.execute.return_value = mock_result

    count = mark_stale_jobs(seen_ids, "test-repo", mock_conn)

    # Should have called execute multiple times (batched) + commits
    assert mock_conn.execute.call_count >= 2, "Should batch large ID sets"
    assert mock_conn.commit.called


def test_mark_stale_small_set_no_batching():
    """When seen_ids is small, no batching needed — single query."""
    seen_ids = {f"job_{i}" for i in range(100)}

    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.rowcount = 3
    mock_conn.execute.return_value = mock_result

    count = mark_stale_jobs(seen_ids, "test-repo", mock_conn)

    # Single execute for small sets
    assert mock_conn.execute.call_count == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && .venv/bin/python -m pytest tests/test_stale_batching.py -v`

Expected: FAIL — `_STALE_BATCH_SIZE` not defined, no batching logic

- [ ] **Step 3: Implement batched stale marking**

Replace `mark_stale_jobs` in `src/wekruit_matching/scraper/upsert.py`:

```python
_STALE_BATCH_SIZE = 5000


def mark_stale_jobs(
    seen_ids: Collection[str],
    source_repo: str,
    conn: psycopg.Connection,
) -> int:
    """Mark active jobs from source_repo as inactive if their job_id is not in seen_ids.

    For large ID sets (>5000), batches the NOT IN query to avoid statement timeouts
    on Supabase's pooler. Each batch commits independently.

    Returns: count of rows marked inactive
    """
    if not seen_ids:
        result = conn.execute(
            """
            UPDATE jobs
            SET status = 'inactive'
            WHERE source_repo = %(source_repo)s AND status = 'active'
            """,
            {"source_repo": source_repo},
        )
        conn.commit()
        count = result.rowcount
        logger.info("Marked {} stale jobs inactive for repo {}", count, source_repo)
        return count

    seen_list = list(seen_ids)
    total_marked = 0

    if len(seen_list) <= _STALE_BATCH_SIZE:
        result = conn.execute(
            """
            UPDATE jobs
            SET status = 'inactive'
            WHERE source_repo = %(source_repo)s
              AND status = 'active'
              AND NOT (job_id = ANY(%(seen_ids)s))
            """,
            {"source_repo": source_repo, "seen_ids": seen_list},
        )
        total_marked = result.rowcount
        conn.commit()
    else:
        # For large sets: collect all active IDs first, then mark those NOT in seen_ids
        active_rows = conn.execute(
            """
            SELECT job_id FROM jobs
            WHERE source_repo = %(source_repo)s AND status = 'active'
            """,
            {"source_repo": source_repo},
        ).fetchall()

        stale_ids = [r["job_id"] for r in active_rows if r["job_id"] not in seen_ids]

        # Batch the UPDATE by stale IDs (smaller set)
        for i in range(0, len(stale_ids), _STALE_BATCH_SIZE):
            batch = stale_ids[i : i + _STALE_BATCH_SIZE]
            result = conn.execute(
                """
                UPDATE jobs
                SET status = 'inactive'
                WHERE job_id = ANY(%(stale_ids)s)
                """,
                {"stale_ids": batch},
            )
            total_marked += result.rowcount
            conn.commit()

    logger.info("Marked {} stale jobs inactive for repo {}", total_marked, source_repo)
    return total_marked
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && .venv/bin/python -m pytest tests/test_stale_batching.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/wekruit_matching/scraper/upsert.py tests/test_stale_batching.py
git commit -m "fix: batch stale-marking queries to avoid statement timeout on large repos"
```

---

### Task 3: Add Mailgun Config Settings

**Files:**
- Modify: `src/wekruit_matching/config.py:11-24`

- [ ] **Step 1: Add Mailgun settings to config**

In `src/wekruit_matching/config.py`, add three optional fields to the `Settings` class:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(...)
    anthropic_api_key: str = Field(..., repr=False)
    openai_api_key: str = Field(..., repr=False)
    github_token: str = Field(..., repr=False)
    log_level: str = Field("INFO")
    api_secret_key: str = Field(..., repr=False)

    # Mailgun (optional — pipeline runs without it, just skips email)
    mailgun_api_key: str = Field("", repr=False)
    mailgun_domain: str = Field("wekruit.com")
    pipeline_notify_email: str = Field("admin1@wekruit.com")
```

- [ ] **Step 2: Add env vars to .env file**

Append to `/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/.env`:

```
# Mailgun notifications
MAILGUN_API_KEY=<to be filled by user>
MAILGUN_DOMAIN=wekruit.com
PIPELINE_NOTIFY_EMAIL=admin1@wekruit.com
```

- [ ] **Step 3: Verify config loads**

Run: `cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && .venv/bin/python -c "from wekruit_matching.config import get_settings; s = get_settings(); print(f'domain={s.mailgun_domain} email={s.pipeline_notify_email}')"`

Expected: `domain=wekruit.com email=admin1@wekruit.com`

- [ ] **Step 4: Commit**

```bash
git add src/wekruit_matching/config.py
git commit -m "feat: add Mailgun config settings for pipeline notifications"
```

---

### Task 4: Create Mailgun Email Notification Module

**Files:**
- Create: `src/wekruit_matching/notifications/__init__.py`
- Create: `src/wekruit_matching/notifications/email.py`
- Create: `tests/test_email_notification.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_email_notification.py`:

```python
"""Verify email notification formatting and graceful degradation."""
from unittest.mock import patch, MagicMock
from wekruit_matching.notifications.email import (
    send_pipeline_start_email,
    send_pipeline_complete_email,
    _format_stats_table,
)


def test_format_stats_table_with_errors():
    stats = {
        "Summer2026-Internships": {"inserted": 99, "updated": 0, "unchanged": 1541, "stale": 125},
        "jobright": {"error": "statement timeout"},
    }
    table = _format_stats_table(stats)
    assert "Summer2026-Internships" in table
    assert "99" in table
    assert "ERROR" in table or "statement timeout" in table


def test_format_stats_table_all_success():
    stats = {
        "repo-a": {"inserted": 10, "updated": 5, "unchanged": 100, "stale": 3},
    }
    table = _format_stats_table(stats)
    assert "repo-a" in table
    assert "10" in table


def test_send_start_email_skips_without_api_key():
    """Should gracefully skip when MAILGUN_API_KEY is empty."""
    with patch("wekruit_matching.notifications.email.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            mailgun_api_key="",
            mailgun_domain="wekruit.com",
            pipeline_notify_email="test@test.com",
        )
        result = send_pipeline_start_email()
        assert result is False


def test_send_complete_email_skips_without_api_key():
    """Should gracefully skip when MAILGUN_API_KEY is empty."""
    with patch("wekruit_matching.notifications.email.get_settings") as mock_settings:
        mock_settings.return_value = MagicMock(
            mailgun_api_key="",
            mailgun_domain="wekruit.com",
            pipeline_notify_email="test@test.com",
        )
        result = send_pipeline_complete_email(
            scrape_stats={},
            enrich_stats={"enriched": 0, "failed": 0, "skipped": 0},
            embed_stats={"embedded": 0, "failed": 0, "skipped": 0},
            duration_seconds=60.0,
            errors=[],
        )
        assert result is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && .venv/bin/python -m pytest tests/test_email_notification.py -v`

Expected: FAIL — module does not exist

- [ ] **Step 3: Create notifications package**

Create `src/wekruit_matching/notifications/__init__.py`:

```python
```

Create `src/wekruit_matching/notifications/email.py`:

```python
"""Mailgun email notifications for the daily pipeline.

Sends start/completion emails via the Mailgun REST API using httpx.
Gracefully skips if MAILGUN_API_KEY is not configured.
"""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
from loguru import logger

from wekruit_matching.config import get_settings


def _format_stats_table(scrape_stats: dict[str, dict]) -> str:
    """Format scrape stats as a plain-text table for email body."""
    lines = []
    lines.append(f"{'Source':<35} {'Inserted':>8} {'Updated':>8} {'Unchanged':>10} {'Stale':>6} {'Status':>10}")
    lines.append("-" * 85)
    for source, stats in scrape_stats.items():
        if "error" in stats:
            lines.append(f"{source:<35} {'—':>8} {'—':>8} {'—':>10} {'—':>6} {'ERROR':>10}")
            lines.append(f"  >> {stats['error']}")
        else:
            lines.append(
                f"{source:<35} {stats.get('inserted', 0):>8} {stats.get('updated', 0):>8} "
                f"{stats.get('unchanged', 0):>10} {stats.get('stale', 0):>6} {'OK':>10}"
            )
    return "\n".join(lines)


def _send_email(subject: str, text: str) -> bool:
    """Send an email via Mailgun REST API. Returns True on success."""
    settings = get_settings()
    if not settings.mailgun_api_key:
        logger.debug("Mailgun not configured — skipping email")
        return False

    try:
        response = httpx.post(
            f"https://api.mailgun.net/v3/{settings.mailgun_domain}/messages",
            auth=("api", settings.mailgun_api_key),
            data={
                "from": f"WeKruit Pipeline <pipeline@{settings.mailgun_domain}>",
                "to": [settings.pipeline_notify_email],
                "subject": subject,
                "text": text,
            },
            timeout=10.0,
        )
        response.raise_for_status()
        logger.info("Email sent: {}", subject)
        return True
    except Exception as e:
        logger.warning("Failed to send email '{}': {}", subject, e)
        return False


def send_pipeline_start_email() -> bool:
    """Send a notification that the daily pipeline has started."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return _send_email(
        subject=f"[WeKruit] Daily pipeline started — {now}",
        text=(
            f"The daily job scraping, enrichment, and embedding pipeline started at {now}.\n\n"
            "Stages:\n"
            "  1. Scrape SimplifyJobs + JobRight GitHub repos\n"
            "  2. Enrich new jobs via Claude Haiku classifier\n"
            "  3. Generate embeddings via OpenAI text-embedding-3-small\n\n"
            "A completion email will follow when the pipeline finishes."
        ),
    )


def send_pipeline_complete_email(
    scrape_stats: dict[str, dict],
    enrich_stats: dict[str, int],
    embed_stats: dict[str, int],
    duration_seconds: float,
    errors: list[str],
) -> bool:
    """Send a notification with pipeline results."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    duration_min = duration_seconds / 60

    total_inserted = sum(
        s.get("inserted", 0) for s in scrape_stats.values() if "error" not in s
    )
    total_stale = sum(
        s.get("stale", 0) for s in scrape_stats.values() if "error" not in s
    )
    has_errors = bool(errors) or any("error" in s for s in scrape_stats.values())
    status = "COMPLETED WITH ERRORS" if has_errors else "COMPLETED OK"

    stats_table = _format_stats_table(scrape_stats)

    error_section = ""
    if errors:
        error_section = "\n\nERRORS:\n" + "\n".join(f"  - {e}" for e in errors)

    text = (
        f"Daily pipeline {status} at {now} ({duration_min:.1f} min)\n\n"
        f"SCRAPING\n{stats_table}\n\n"
        f"Total new jobs: {total_inserted} | Stale removed: {total_stale}\n\n"
        f"ENRICHMENT\n"
        f"  Enriched: {enrich_stats.get('enriched', 0)} | "
        f"Failed: {enrich_stats.get('failed', 0)}\n\n"
        f"EMBEDDING\n"
        f"  Embedded: {embed_stats.get('embedded', 0)} | "
        f"Failed: {embed_stats.get('failed', 0)}"
        f"{error_section}"
    )

    return _send_email(
        subject=f"[WeKruit] Pipeline {status} — {total_inserted} new jobs ({now})",
        text=text,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && .venv/bin/python -m pytest tests/test_email_notification.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/wekruit_matching/notifications/ tests/test_email_notification.py
git commit -m "feat: add Mailgun email notifications for pipeline start/completion"
```

---

### Task 5: Create Unified Daily Pipeline Orchestrator

**Files:**
- Create: `src/wekruit_matching/pipeline/__init__.py`
- Create: `src/wekruit_matching/pipeline/daily.py`

- [ ] **Step 1: Create pipeline package**

Create `src/wekruit_matching/pipeline/__init__.py`:

```python
```

- [ ] **Step 2: Create unified daily orchestrator**

Create `src/wekruit_matching/pipeline/daily.py`:

```python
"""Unified daily pipeline orchestrator.

Runs scrape -> enrich -> embed in sequence, captures stats and errors,
and sends email notifications on start and completion.

Standalone CLI usage:
    uv run python -m wekruit_matching.pipeline.daily

Replaces the fragmented daily-update.sh + inline launchd commands.
"""
import sys
import time

from loguru import logger

from wekruit_matching.embedding.run import embed_all
from wekruit_matching.enrichment.run import enrich_all
from wekruit_matching.notifications.email import (
    send_pipeline_complete_email,
    send_pipeline_start_email,
)
from wekruit_matching.scraper.run import scrape_all


def run_daily_pipeline() -> dict:
    """Execute the full daily pipeline with email notifications.

    Returns a dict with all stage stats and any errors encountered.
    """
    start = time.monotonic()
    errors: list[str] = []

    # --- Notify: start ---
    send_pipeline_start_email()

    # --- Stage 1: Scrape ---
    logger.info("=== Stage 1: Scraping ===")
    try:
        scrape_stats = scrape_all()
        logger.info("Scrape stats: {}", scrape_stats)
        # Collect scrape-level errors
        for source, stats in scrape_stats.items():
            if "error" in stats:
                errors.append(f"Scrape {source}: {stats['error']}")
    except Exception as e:
        logger.error("Scraper crashed: {}", e)
        scrape_stats = {"pipeline": {"error": str(e)}}
        errors.append(f"Scraper crash: {e}")

    # --- Stage 2: Enrich ---
    logger.info("=== Stage 2: Enrichment ===")
    try:
        enrich_stats = enrich_all()
        logger.info("Enrich stats: {}", enrich_stats)
    except Exception as e:
        logger.error("Enrichment crashed: {}", e)
        enrich_stats = {"enriched": 0, "failed": 0, "skipped": 0}
        errors.append(f"Enrichment crash: {e}")

    # --- Stage 3: Embed ---
    logger.info("=== Stage 3: Embedding ===")
    try:
        embed_stats = embed_all()
        logger.info("Embed stats: {}", embed_stats)
    except Exception as e:
        logger.error("Embedding crashed: {}", e)
        embed_stats = {"embedded": 0, "failed": 0, "skipped": 0}
        errors.append(f"Embedding crash: {e}")

    duration = time.monotonic() - start

    # --- Notify: complete ---
    send_pipeline_complete_email(
        scrape_stats=scrape_stats,
        enrich_stats=enrich_stats,
        embed_stats=embed_stats,
        duration_seconds=duration,
        errors=errors,
    )

    return {
        "scrape": scrape_stats,
        "enrich": enrich_stats,
        "embed": embed_stats,
        "errors": errors,
        "duration_seconds": duration,
    }


if __name__ == "__main__":
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting daily pipeline")
    result = run_daily_pipeline()
    logger.info("Daily pipeline complete. Duration: {:.1f}m", result["duration_seconds"] / 60)
    if result["errors"]:
        logger.warning("Errors: {}", result["errors"])
        sys.exit(1)
```

- [ ] **Step 3: Commit**

```bash
git add src/wekruit_matching/pipeline/
git commit -m "feat: unified daily pipeline orchestrator with email notifications"
```

---

### Task 6: Update Shell Script and LaunchD Plist

**Files:**
- Modify: `scripts/daily-update.sh`
- Modify: `~/Library/LaunchAgents/com.wekruit.daily-update.plist`

- [ ] **Step 1: Simplify daily-update.sh**

Replace `scripts/daily-update.sh`:

```bash
#!/bin/bash
# Daily job pipeline: scrape, enrich, embed + email notifications
# Runs via launchd at 6 AM CDT daily
cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching

.venv/bin/python -m wekruit_matching.pipeline.daily
```

- [ ] **Step 2: Update launchd plist to use shell script and merge log streams**

Replace `~/Library/LaunchAgents/com.wekruit.daily-update.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.wekruit.daily-update</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>/Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching/scripts/daily-update.sh</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/tmp/matching-daily-update.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/matching-daily-update.log</string>
</dict>
</plist>
```

Key changes:
- Uses the shell script instead of inline commands (single source of truth)
- Both stdout and stderr go to the same `.log` file (fixes empty log issue)

- [ ] **Step 3: Reload launchd agent**

```bash
launchctl unload ~/Library/LaunchAgents/com.wekruit.daily-update.plist
launchctl load ~/Library/LaunchAgents/com.wekruit.daily-update.plist
launchctl list | grep daily-update
```

Expected: `- 0 com.wekruit.daily-update`

- [ ] **Step 4: Commit**

```bash
git add scripts/daily-update.sh
git commit -m "fix: unify daily pipeline via orchestrator module, merge log streams in launchd"
```

---

### Task 7: Add Mailgun API Key to .env

**Files:**
- Modify: `wekruit-matching/.env`

- [ ] **Step 1: Prompt user for Mailgun API key**

The user said Mailgun is already integrated. The API key needs to be added to the wekruit-matching `.env` file.

Check existing VALET `.env` files for the Mailgun API key and copy it:

```bash
# Check if Mailgun key exists in VALET's Fly.io secrets
fly secrets list -a valet-api 2>/dev/null | grep MAILGUN

# Or check Infisical
# The user should provide or confirm the API key
```

Add to `.env`:

```
MAILGUN_API_KEY=<key from VALET or Infisical>
```

- [ ] **Step 2: Verify end-to-end by sending a test email**

```bash
cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching
.venv/bin/python -c "
from wekruit_matching.notifications.email import send_pipeline_start_email
result = send_pipeline_start_email()
print(f'Email sent: {result}')
"
```

Expected: `Email sent: True` (or `False` if key not yet configured)

---

### Task 8: Run All Tests

- [ ] **Step 1: Run full test suite**

```bash
cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching
.venv/bin/python -m pytest tests/ -v
```

Expected: All tests pass

- [ ] **Step 2: Manual smoke test — run the pipeline**

```bash
cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching
.venv/bin/python -m wekruit_matching.pipeline.daily 2>&1 | tail -20
```

Expected: Pipeline runs all 3 stages, no statement timeout on stale marking, email sent on start + completion
