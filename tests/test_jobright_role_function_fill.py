"""jobright scraper must fill role_function + seniority_level at scrape time.

2026-05-21 launch-blocker: jobright `_to_job` had not been calling
`infer_role_function(title)` or `infer_seniority(title)` like
ashby_direct/greenhouse_direct/lever_direct do. Result: 89% of
jobright-newgrad rows landed with empty role_function — invisible to
function-filtered matching queries.

These contract tests pin:
  * Software-engineering titles → role_function includes
    "software_engineering"
  * Internship titles → seniority_level == "intern"
  * Empty/unknown titles → empty role_function (we never fabricate)
"""
from __future__ import annotations

from wekruit_matching.scraper.jobright import _to_job


def _raw(title: str, company: str = "Acme Corp", **kwargs) -> dict:
    base = {
        "title": title,
        "company": company,
        "applyUrl": "https://acme.example/job",
        "location": "San Francisco, CA",
        "salary": "",
        "qualifications": "Python and Go.",
        "industry": [],
        "companySize": "",
        "h1bSponsored": None,
    }
    base.update(kwargs)
    return base


def test_jobright_to_job_fills_role_function_for_swe_title() -> None:
    job = _to_job(_raw("Software Engineer, Backend"), "jobright-newgrad")
    assert job is not None
    assert "software_engineering" in job.role_function


def test_jobright_to_job_fills_seniority_for_intern_title() -> None:
    job = _to_job(_raw("Software Engineer Intern"), "jobright-newgrad")
    assert job is not None
    assert job.seniority_level == "intern"


def test_jobright_to_job_fills_seniority_for_new_grad_title() -> None:
    job = _to_job(_raw("New Grad Software Engineer"), "jobright-newgrad")
    assert job is not None
    assert job.seniority_level in {"new_grad", "entry", "entry_level"}


def test_jobright_to_job_role_function_empty_when_inference_blank() -> None:
    """Generic non-matching title → empty role_function, NOT fabricated."""
    job = _to_job(_raw("xyzzy fnord plough"), "jobright-newgrad")
    assert job is not None
    # role_function is a list[str]; may be empty if no rule matches —
    # we accept empty but never null fabrications. Pydantic default is
    # empty list.
    assert job.role_function == [] or job.role_function is None


def test_jobright_to_job_returns_none_for_blank_title() -> None:
    assert _to_job(_raw(""), "jobright-newgrad") is None
    assert _to_job(_raw("title", company=""), "jobright-newgrad") is None
