"""Git-clone-backed scraper for jobright-ai GitHub repos.

Replaces the per-repo HTTP fetch in `jobright_github.py` with a local
git clone + `git pull`. Optionally identifies newly-added markdown table
rows via `git diff HEAD~1..HEAD README.md` for fast-delta scrapes (gated
by ``JOBRIGHT_USE_GIT_DELTA=1``).

Why this exists
---------------
jobright-ai maintains ~34 GitHub repos under https://github.com/jobright-ai
(one per role-category × intern/newgrad), each shipping a daily-committed
README.md with the listing table. Pre-v2 the pipeline re-fetched all 34
READMEs via HTTPS every day and re-parsed each row, generating phantom
Firestore docs because the embedded redirect URLs rotated per-commit
(see `id_utils.py` v2 docstring for the 70k phantom incident).

v2 (stable_job_id) already fixed correctness. THIS module is the
performance layer: clone once, `git pull` daily, only parse newly-added
table rows. Falls back gracefully to a full local-README parse if the
diff is missing or too large (force-push / first run).

Activation
----------
Set ``JOBRIGHT_USE_GIT_DELTA=1`` in macmini env to flip the scraper from
HTTP fetch → git delta. ``JOBRIGHT_CLONE_ROOT`` overrides the on-disk
clone location (default ``/Users/Shared/wekruit/jobright-repos``).

Output contract
---------------
``fetch_repo(repo_name)`` returns a `RepoSnapshot` with:
  * ``full_readme``  — current README.md content (always set)
  * ``added_rows``   — markdown table rows added in HEAD~1..HEAD (may be
                       ``None`` on first clone / force-push / no prior
                       commit)
  * ``used_delta``   — True if `added_rows` is a trustworthy delta
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

JOBRIGHT_ORG = "jobright-ai"
CLONE_ROOT = Path(os.environ.get("JOBRIGHT_CLONE_ROOT", "/Users/Shared/wekruit/jobright-repos"))
MAX_DELTA_LINES = 1000  # diff larger than this = force-push, fall back to full parse


@dataclass
class RepoSnapshot:
    repo_name: str
    full_readme: str
    added_rows: list[str] | None
    used_delta: bool


def _run(args: list[str], cwd: Path | None = None) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def ensure_clone(repo_name: str) -> Path:
    """Clone github.com/jobright-ai/{repo_name} if missing, else pull origin/HEAD.

    Returns the local clone path. ``--depth 30`` keeps clones small while
    leaving enough history for HEAD~1 diffs.
    """
    CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    repo_dir = CLONE_ROOT / repo_name
    url = f"https://github.com/{JOBRIGHT_ORG}/{repo_name}.git"
    if not (repo_dir / ".git").exists():
        logger.info("git clone %s", url)
        _run(["git", "clone", "--depth", "30", url, str(repo_dir)])
    else:
        # Reset any local drift, then pull fresh history.
        _run(["git", "fetch", "origin", "--depth", "30"], cwd=repo_dir)
        # Resolve the remote default branch instead of assuming `main`/`master`.
        head_ref = _run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=repo_dir,
        ).strip() or "origin/main"
        _run(["git", "reset", "--hard", head_ref], cwd=repo_dir)
    return repo_dir


def _diff_added_rows(repo_dir: Path, file_path: str = "README.md") -> list[str] | None:
    """Return rows added in HEAD~1..HEAD that look like markdown table lines.

    Returns ``None`` when there is no prior commit (fresh shallow clone),
    or when the diff exceeds MAX_DELTA_LINES (treat as force-push).
    """
    try:
        diff = _run(["git", "diff", "HEAD~1..HEAD", "--unified=0", "--", file_path], cwd=repo_dir)
    except RuntimeError as e:
        logger.warning("git diff failed for %s: %s — falling back to full parse", repo_dir.name, e)
        return None
    added: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        row = line[1:].strip()
        if row.startswith("|") and row.endswith("|"):
            added.append(row)
        if len(added) > MAX_DELTA_LINES:
            logger.warning(
                "%s: delta exceeded %d lines — likely force-push, falling back to full parse",
                repo_dir.name, MAX_DELTA_LINES,
            )
            return None
    return added


def fetch_repo(repo_name: str) -> RepoSnapshot:
    """Clone/pull repo + return current README + (optional) delta rows.

    Always succeeds (clone failures bubble up as `RuntimeError`). Callers
    use ``snapshot.used_delta`` to decide whether to parse only delta rows
    or the full README.
    """
    repo_dir = ensure_clone(repo_name)
    readme_path = repo_dir / "README.md"
    if not readme_path.exists():
        raise RuntimeError(f"{repo_name}: README.md missing after clone")
    full_readme = readme_path.read_text(encoding="utf-8")

    added_rows = _diff_added_rows(repo_dir)
    used_delta = added_rows is not None
    logger.info(
        "fetch_repo %s: full=%dB delta_rows=%s",
        repo_name, len(full_readme), len(added_rows) if added_rows else "n/a",
    )
    return RepoSnapshot(repo_name=repo_name, full_readme=full_readme,
                        added_rows=added_rows, used_delta=used_delta)


def is_enabled() -> bool:
    return os.environ.get("JOBRIGHT_USE_GIT_DELTA") == "1"
