"""Unit test: scrape_board retries a thin/partial Firecrawl render with a longer
wait, and keeps the best attempt.

2026-06-03: VC boards intermittently returned thin/partial markdown (the SPA had
not finished hydrating within waitFor), so we under-captured jobs and the
mark_stale circuit-breaker tripped ("partial render, skipped deactivation").
scrape_board now retries on a thin render. Offline — Firecrawl HTTP is mocked.
"""

from __future__ import annotations

from wekruit_matching.scraper.vc_board import (
    MIN_RENDER_MARKDOWN_CHARS,
    RENDER_ATTEMPT_WAIT_MULTIPLIERS,
    FirecrawlClient,
    VCBoardConfig,
    scrape_board,
)

# Markdown that parses to exactly one Job: an H4 job heading whose URL carries
# /companies/<slug>/jobs/<id>, plus the sibling company link (no /jobs/). Padded
# past MIN_RENDER_MARKDOWN_CHARS so it counts as a complete render.
_FULL_MD = (
    "#### [Software Engineer](https://jobs.example.com/companies/acme/jobs/123-swe#content)\n\n"
    "[Acme](https://jobs.example.com/companies/acme)\n\n"
    + ("filler line to exceed the min-render char floor. " * 20)
)
_THIN_MD = "loading…"  # well under MIN_RENDER_MARKDOWN_CHARS, zero jobs

_BOARD = VCBoardConfig("test", "https://board.example.com/jobs", "Test VC", wait_ms=4000)


class _Resp:
    def __init__(self, markdown: str, status: int = 200, success: bool = True):
        self.status_code = status
        self._md = markdown
        self._success = success
        self.text = markdown

    def json(self) -> dict:
        return {"success": self._success, "data": {"markdown": self._md}}


def _seq_post(responses: list[_Resp]):
    """A fake httpx.post that returns queued responses and counts calls + waits."""
    state = {"n": 0, "waits": []}

    def post(url, headers=None, json=None, timeout=None):
        state["waits"].append(json.get("waitFor"))
        i = state["n"]
        state["n"] += 1
        return responses[min(i, len(responses) - 1)]

    post.state = state
    return post


def _client(responses: list[_Resp]) -> tuple[FirecrawlClient, dict]:
    post = _seq_post(responses)
    return FirecrawlClient(base_url="http://firecrawl.test", http_post=post), post.state


def test_full_render_first_attempt_does_not_retry():
    client, state = _client([_Resp(_FULL_MD)])
    jobs = scrape_board(client, _BOARD)
    assert len(jobs) == 1, jobs
    assert state["n"] == 1, "a complete render must not retry"


def test_thin_then_full_retries_and_recovers():
    client, state = _client([_Resp(_THIN_MD), _Resp(_FULL_MD)])
    jobs = scrape_board(client, _BOARD)
    assert len(jobs) == 1, jobs
    assert state["n"] == 2, "a thin render must trigger one retry"
    # The retry used a longer wait than the first attempt.
    assert state["waits"][1] > state["waits"][0], state["waits"]


def test_all_thin_returns_best_effort_without_raising():
    client, state = _client([_Resp(_THIN_MD), _Resp(_THIN_MD)])
    jobs = scrape_board(client, _BOARD)
    assert jobs == [], jobs
    assert state["n"] == len(RENDER_ATTEMPT_WAIT_MULTIPLIERS)


def test_min_render_floor_is_positive():
    # Guard the heuristic constant so a future edit can't silently disable it.
    assert MIN_RENDER_MARKDOWN_CHARS > 0
    assert RENDER_ATTEMPT_WAIT_MULTIPLIERS[0] == 1.0
    assert len(RENDER_ATTEMPT_WAIT_MULTIPLIERS) >= 2
