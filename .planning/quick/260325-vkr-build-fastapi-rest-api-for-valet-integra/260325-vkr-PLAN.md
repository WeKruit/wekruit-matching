---
phase: quick
plan: 260325-vkr
type: execute
wave: 1
depends_on: []
files_modified:
  - pyproject.toml
  - src/wekruit_matching/api/__init__.py
  - src/wekruit_matching/api/server.py
autonomous: true
requirements: [VKR-API]

must_haves:
  truths:
    - "POST /match accepts a UserProfile JSON body and returns a ranked list of job dicts with score and signals"
    - "POST /feedback accepts user_id, job_id, reaction and records the reaction via record_feedback"
    - "GET /jobs/stats returns job counts grouped by source_repo and status"
    - "Server starts with `uv run uvicorn wekruit_matching.api.server:app` and responds on localhost:8000"
  artifacts:
    - path: "src/wekruit_matching/api/server.py"
      provides: "FastAPI app with three endpoints"
      exports: ["app"]
    - path: "src/wekruit_matching/api/__init__.py"
      provides: "Package marker"
    - path: "pyproject.toml"
      provides: "fastapi and uvicorn dependencies"
      contains: "fastapi"
  key_links:
    - from: "POST /match"
      to: "wekruit_matching.matching.matcher.get_matches"
      via: "direct import — passes UserProfile parsed from request body"
    - from: "POST /feedback"
      to: "wekruit_matching.feedback.handler.record_feedback"
      via: "direct import — passes user_id, job_id, reaction from FeedbackRequest body"
    - from: "GET /jobs/stats"
      to: "psycopg3 connection pool via get_connection()"
      via: "SELECT source_repo, status, COUNT(*) FROM jobs GROUP BY source_repo, status"
---

<objective>
Wrap the existing matching engine as a FastAPI HTTP REST API for VALET integration.

Purpose: VALET needs an HTTP interface to call the matching engine. The matching engine is already complete as a Python library — this plan adds a thin HTTP layer on top without changing any internals.
Output: src/wekruit_matching/api/server.py exposing three endpoints; fastapi + uvicorn added to pyproject.toml dependencies.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md
@.planning/quick/260325-vkr-build-fastapi-rest-api-for-valet-integra/260325-vkr-PLAN.md

<interfaces>
<!-- Key contracts the executor needs. No codebase exploration required. -->

From src/wekruit_matching/__init__.py:
```python
from wekruit_matching.matching.matcher import get_matches
from wekruit_matching.feedback.handler import record_feedback
```

From src/wekruit_matching/matching/matcher.py:
```python
def get_matches(
    profile: UserProfile,
    conn: psycopg.Connection | None = None,
    top_n: int = 30,
    openai_client: openai.OpenAI | None = None,
) -> list[dict]:
    # Returns list of job dicts, each containing all job DB fields plus:
    #   "score": float (0.0–1.0)
    #   "signals": dict[str, float]
```

From src/wekruit_matching/feedback/handler.py:
```python
def record_feedback(
    user_id: str,
    job_id: str,
    reaction: str | ReactionType,
    conn: psycopg.Connection | None = None,
) -> None:
    # record_feedback does NOT commit — it uses get_connection() internally
    # which handles connection lifecycle. Commit IS called inside _run via get_connection context.
```

From src/wekruit_matching/models/user_profile.py:
```python
class JobType(str, Enum):
    INTERN = "intern"
    NEW_GRAD = "new_grad"
    ANY = "any"

class CompanySizePreference(str, Enum):
    STARTUP = "startup"
    MIDSIZE = "midsize"
    LARGE = "large"
    ANY = "any"

class UserProfile(BaseModel):
    user_id: str
    skills: list[str] = []
    preferred_job_type: JobType = JobType.ANY
    preferred_locations: list[str] = []
    requires_sponsorship: bool = False
    preferred_company_size: CompanySizePreference = CompanySizePreference.ANY
    preferred_industries: list[str] = []
    liked_companies: list[str] = []
    disliked_companies: list[str] = []
    affinity_embedding: Optional[list[float]] = None
```

From src/wekruit_matching/models/feedback.py:
```python
class ReactionType(str, Enum):
    LIKE = "like"
    DISLIKE = "dislike"
    APPLIED = "applied"
```

From src/wekruit_matching/db/connection.py:
```python
@contextmanager
def get_connection() -> Generator[psycopg.Connection, None, None]:
    # Yields a psycopg3 connection from the pool (dict_row factory)
```
</interfaces>
</context>

<tasks>

<task type="auto">
  <name>Task 1: Add fastapi and uvicorn to dependencies</name>
  <files>pyproject.toml</files>
  <action>
    Add two entries to the `dependencies` list in pyproject.toml:
      - `"fastapi>=0.115"` (0.115 is the current stable line; includes Pydantic v2 integration)
      - `"uvicorn>=0.29"` (ASGI server for running the FastAPI app)

    Run `uv add fastapi uvicorn` from the project root so uv.lock is updated alongside pyproject.toml.

    Do NOT add `fastapi[standard]` — the standard extra pulls in email-validator and other extras that are not needed here; a bare fastapi install is sufficient.
  </action>
  <verify>
    <automated>cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && uv run python -c "import fastapi; import uvicorn; print('ok')"</automated>
  </verify>
  <done>Both fastapi and uvicorn importable in the project venv. pyproject.toml and uv.lock updated.</done>
</task>

<task type="auto">
  <name>Task 2: Create FastAPI server with three endpoints</name>
  <files>
    src/wekruit_matching/api/__init__.py
    src/wekruit_matching/api/server.py
  </files>
  <action>
    Create `src/wekruit_matching/api/__init__.py` as an empty file (package marker only).

    Create `src/wekruit_matching/api/server.py` with the following structure:

    **Imports:**
    ```python
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel
    from wekruit_matching.matching.matcher import get_matches
    from wekruit_matching.feedback.handler import record_feedback
    from wekruit_matching.models.user_profile import UserProfile
    from wekruit_matching.models.feedback import ReactionType
    from wekruit_matching.db.connection import get_connection
    ```

    **App instantiation:**
    ```python
    app = FastAPI(title="WeKruit Matching API", version="0.1.0")
    ```

    **POST /match:**
    - Request body: `UserProfile` (reuse the existing Pydantic model directly as the request body — FastAPI handles this automatically)
    - Call `get_matches(profile)` — uses default `top_n=30`, `conn=None` (uses pool)
    - Return body: `{"matches": <list of dicts from get_matches>}` — wrap in a dict so the response is a JSON object, not a bare array
    - On any exception from get_matches, raise `HTTPException(status_code=500, detail=str(e))`

    **POST /feedback — request model (define inline in server.py):**
    ```python
    class FeedbackRequest(BaseModel):
        user_id: str
        job_id: str
        reaction: ReactionType
    ```
    - Call `record_feedback(body.user_id, body.job_id, body.reaction)`
    - Return `{"status": "ok"}` on success
    - On any exception, raise `HTTPException(status_code=500, detail=str(e))`
    - Note: record_feedback uses get_connection() internally and handles the connection/commit lifecycle itself — no manual connection management needed in the endpoint

    **GET /jobs/stats:**
    - Open a connection via `with get_connection() as conn:`
    - Execute: `SELECT source_repo, status, COUNT(*) as count FROM jobs GROUP BY source_repo, status ORDER BY source_repo, status`
    - Collect rows into a list of dicts: `[{"source_repo": row["source_repo"], "status": row["status"], "count": row["count"]} for row in rows]`
    - Return `{"stats": <list>}`
    - On any exception, raise `HTTPException(status_code=500, detail=str(e))`

    **Important implementation notes:**
    - Do NOT add lifespan startup/shutdown hooks or pool pre-warming — get_pool() is lru_cached and initializes lazily on first request; this is correct for a VALET integration server
    - Do NOT add auth middleware or API keys — this is an internal service for VALET
    - Do NOT use async def for endpoints — psycopg3 ConnectionPool is synchronous; mixing sync DB calls into async FastAPI endpoints requires `asyncio.to_thread` or switching to AsyncConnectionPool; use `def` (sync) endpoints so FastAPI runs them in a threadpool automatically
    - Use `except Exception as e:` in each endpoint handler — do not let DB or matching errors surface as unhandled 500s without a message
  </action>
  <verify>
    <automated>cd /Users/wekruitclaw1/Desktop/WeKruit/wekruit-matching && uv run python -c "from wekruit_matching.api.server import app; from fastapi.testclient import TestClient; print('import ok')"</automated>
  </verify>
  <done>
    - `src/wekruit_matching/api/__init__.py` exists
    - `src/wekruit_matching/api/server.py` exists with `app` exported
    - `from wekruit_matching.api.server import app` imports without error
    - `uv run uvicorn wekruit_matching.api.server:app` starts the server (verify manually by running and hitting Ctrl-C after seeing "Uvicorn running on http://127.0.0.1:8000")
    - OpenAPI docs available at http://127.0.0.1:8000/docs showing all three endpoints
  </done>
</task>

</tasks>

<verification>
After both tasks complete:
1. `uv run python -c "from wekruit_matching.api.server import app; print(app.title)"` prints "WeKruit Matching API"
2. `uv run uvicorn wekruit_matching.api.server:app --host 0.0.0.0 --port 8000` starts without ImportError or configuration errors
3. All three routes visible in OpenAPI schema: POST /match, POST /feedback, GET /jobs/stats
</verification>

<success_criteria>
- fastapi and uvicorn present in pyproject.toml dependencies and importable via uv run
- src/wekruit_matching/api/server.py exists with FastAPI app object named `app`
- POST /match accepts UserProfile JSON, returns {"matches": [...]}
- POST /feedback accepts {user_id, job_id, reaction}, returns {"status": "ok"}
- GET /jobs/stats returns {"stats": [{source_repo, status, count}, ...]}
- Server starts with: `uv run uvicorn wekruit_matching.api.server:app`
</success_criteria>

<output>
After completion, create `.planning/quick/260325-vkr-build-fastapi-rest-api-for-valet-integra/260325-vkr-SUMMARY.md`
</output>
