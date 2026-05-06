"""Shared title-based inference helpers for scrapers.

Consolidates SENIORITY_REGEX (previously duplicated across
greenhouse_direct.py / lever_direct.py / ashby_direct.py / linkedin.py /
wellfound.py / otta.py) into a single authoritative implementation, and adds
``infer_role_function()`` which classifies a free-text title into the
canonical jobright ``utm_campaign`` 17-value vocab used by wekruit-pa
v1.6 (`packages/shared-tags/src/canonical/role-function.ts`).

Why split out:
    The downstream V16 matcher (queryMatchingJobs in wekruit-pa) does an
    array-contains-any hard-filter on `roleFunction`. Without this field
    being set at scrape time, ALL career-ops jobs (Greenhouse / Lever /
    Ashby) get filtered out, regardless of how good a JD-match they are.
    See P10 audit (2026-05-06) for full context.

Both helpers are pure functions — no I/O, no LLM calls — so they're cheap
enough to call inside the inner scraper loop on every job.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Seniority — Phase 52 careerStage vocab.
# Returns one of: c_level | vp | director | manager | principal | staff |
# senior | mid_level | junior | entry_level | intern. Defaults to mid_level.
# ---------------------------------------------------------------------------

SENIORITY_REGEX: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(c[teafmid]o|cxo)\b", re.IGNORECASE), "c_level"),
    (re.compile(r"\bchief\s+\w+(\s+\w+)?\s+officer\b", re.IGNORECASE), "c_level"),
    (re.compile(r"\b(vp|vice\s*president)\b", re.IGNORECASE), "vp"),
    (re.compile(r"\b(director|head\s+of)\b", re.IGNORECASE), "director"),
    # Principal / staff must come BEFORE the broader manager/architect pattern,
    # otherwise "Principal Architect" → manager (architect wins). Order is
    # intentional — most-specific seniority modifier wins.
    (re.compile(r"\bprincipal\b", re.IGNORECASE), "principal"),
    (re.compile(r"\bstaff\b", re.IGNORECASE), "staff"),
    (
        re.compile(
            r"\b(manager|engineering\s+lead|tech\s+lead|team\s+lead|architect)\b",
            re.IGNORECASE,
        ),
        "manager",
    ),
    (re.compile(r"\b(intern(ship)?|co-?op)\b", re.IGNORECASE), "intern"),
    (
        re.compile(
            r"\b(new\s*grad(uate)?|entry[\s-]*level|early\s*career)\b",
            re.IGNORECASE,
        ),
        "entry_level",
    ),
    (
        re.compile(
            r"\b(senior|sr\.?\s+(eng(ineer)?|developer|analyst|manager|associate|consultant|designer|scientist|architect|director))\b",
            re.IGNORECASE,
        ),
        "senior",
    ),
    (
        re.compile(
            r"\b(junior|jr\.?\s+(eng(ineer)?|developer|analyst|associate|consultant))\b",
            re.IGNORECASE,
        ),
        "junior",
    ),
]


def infer_seniority(title: str) -> str:
    """Map a free-text title to a canonical Phase 52 careerStage bucket.

    Falls back to ``"mid_level"`` when no pattern matches — most plain
    "Software Engineer" / "Account Executive" listings are mid-level IC.
    """
    if not title or not isinstance(title, str):
        return "mid_level"
    for pattern, level in SENIORITY_REGEX:
        if pattern.search(title):
            return level
    return "mid_level"


# ---------------------------------------------------------------------------
# Role function — jobright `utm_campaign` 17 verbatim, matching the canonical
# vocab in wekruit-pa `packages/shared-tags/src/canonical/role-function.ts`.
# Multi-pick allowed (returns list[str]). Empty list = unmatched, downstream
# V16 hard filter will drop the row.
# ---------------------------------------------------------------------------
#
# Order matters: most-specific patterns first. A "Software Engineer" title
# must match `software_engineering` before the generic "engineer" fallback
# in `engineering_and_development`. A "Data Engineer" title must match
# `data_analysis` (combines ML/DS/DA/DE in jobright's bucket) before the
# software_engineering pattern, so ROLE_FUNCTION_REGEX lists data_*
# before software_*.

ROLE_FUNCTION_REGEX: list[tuple[re.Pattern[str], list[str]]] = [
    # Data / ML / Analytics — must come BEFORE software_engineering so
    # "Data Engineer" / "ML Engineer" don't fall through to SWE.
    (
        re.compile(
            r"\b(data\s+(scientist|engineer|analyst|architect)|"
            r"ml(\s|-)*engineer|machine\s*learning(\s+engineer)?|"
            r"applied\s+scientist|research\s+scientist|"
            r"analytics(\s+(engineer|manager|lead))?|"
            r"business\s+intelligence|bi\s+(analyst|engineer|developer)|"
            r"quant(itative)?(\s+(researcher|analyst|developer))?)\b",
            re.IGNORECASE,
        ),
        ["data_analysis"],
    ),
    # Software / engineering / dev — broad net for IC technical roles.
    (
        re.compile(
            r"\b(software\s+engineer(ing)?|swe|sde|"
            r"backend|front[\s-]*end|full[\s-]*stack|"
            r"devops|sre|site\s+reliability|"
            r"platform\s+(engineer|developer)|cloud\s+engineer|"
            r"security\s+(engineer|architect)|"
            r"infrastructure\s+engineer|"
            r"mobile\s+(engineer|developer)|ios\s+(engineer|developer)|android\s+(engineer|developer)|"
            r"web\s+(engineer|developer)|"
            r"developer|programmer|"
            r"software\s+architect|systems?\s+engineer|"
            r"qa\s+engineer|test\s+engineer|sdet|"
            r"firmware\s+engineer|embedded\s+engineer|"
            r"architect|software\s+architect|cloud\s+architect|solutions\s+architect|"
            r"data\s+architect|enterprise\s+architect)\b",
            re.IGNORECASE,
        ),
        ["software_engineering"],
    ),
    # Hardware / EE / mechanical / civil etc — non-software engineering bucket.
    (
        re.compile(
            r"\b(hardware\s+engineer|mechanical\s+engineer|electrical\s+engineer|"
            r"civil\s+engineer|chemical\s+engineer|"
            r"manufacturing\s+engineer|materials\s+engineer|"
            r"aerospace\s+engineer|biomedical\s+engineer|industrial\s+engineer|"
            r"electronics\s+engineer)\b",
            re.IGNORECASE,
        ),
        ["engineering_and_development"],
    ),
    # Product Management.
    (
        re.compile(
            r"\b(product\s+(manager|owner|lead|director|vp)|"
            r"associate\s+product\s+manager|apm|"
            r"technical\s+product\s+manager|tpm|"
            r"\bpm\b)\b",
            re.IGNORECASE,
        ),
        ["product_management"],
    ),
    # Design / creative.
    (
        re.compile(
            r"\b(designer|design\s+(lead|manager|director)|"
            r"ux\s+(designer|researcher|writer)|ui\s+designer|"
            r"product\s+designer|visual\s+designer|graphic\s+designer|"
            r"motion\s+designer|brand\s+designer|"
            r"illustrator|creative\s+(director|lead))\b",
            re.IGNORECASE,
        ),
        ["creatives_and_design"],
    ),
    # Marketing / growth.
    (
        re.compile(
            r"\b(marketing(\s+(manager|lead|specialist|coordinator|director))?|"
            r"growth(\s+(manager|lead|marketer|hacker))?|"
            r"seo(\s+(specialist|manager|analyst))?|sem|"
            r"content\s+(strategist|marketer|manager|writer|creator)|"
            r"brand\s+(manager|strategist)|"
            r"social\s+media(\s+(manager|specialist|coordinator))?|"
            r"demand\s+gen(eration)?|"
            r"performance\s+marketing|paid\s+(media|search|social)|"
            r"email\s+marketing|product\s+marketing|"
            r"influencer\s+marketing|community\s+(manager|lead))\b",
            re.IGNORECASE,
        ),
        ["marketing"],
    ),
    # Sales / business development.
    (
        re.compile(
            r"\b(sales(\s+(rep(resentative)?|associate|executive|"
            r"manager|director|lead|consultant|engineer|specialist))?|"
            r"account\s+executive|\bae\b|"
            r"business\s+development(\s+(rep(resentative)?|manager|director|lead))?|"
            r"\bbdr\b|\bsdr\b|"
            r"sales\s+development(\s+(rep(resentative)?))?|"
            r"inside\s+sales|outside\s+sales|enterprise\s+sales|"
            r"channel\s+(sales|partner)|partnerships?\s+(manager|lead|director))\b",
            re.IGNORECASE,
        ),
        ["sales"],
    ),
    # Customer success / support.
    (
        re.compile(
            r"\b(customer\s+(success|support|service|experience|"
            r"care|operations)(\s+(rep(resentative)?|manager|lead|"
            r"specialist|engineer|associate))?|"
            r"\bcsm\b|cx\s+(manager|lead)|"
            r"account\s+manager|technical\s+account\s+manager|tam|"
            r"support\s+(engineer|specialist|technician|agent)|"
            r"client\s+(success|services|partner))\b",
            re.IGNORECASE,
        ),
        ["customer_service_and_support"],
    ),
    # HR / recruiting / people.
    (
        re.compile(
            r"\b(recruiter|recruiting(\s+(manager|lead|coordinator))?|"
            r"talent\s+(acquisition|partner|sourcer|manager|lead)|"
            r"\bhr\b|human\s+resources|"
            r"hr(\s|-)*business\s+partner|hrbp|"
            r"people\s+(operations|partner|ops|manager|lead)|"
            r"chief\s+people\s+officer|cpo\s+(people)|"
            r"compensation(\s+(analyst|manager))?|"
            r"benefits\s+(specialist|manager)|"
            r"learning\s+and\s+development|l&d|"
            r"diversity\s+(and|&)\s+inclusion|dei)\b",
            re.IGNORECASE,
        ),
        ["human_resources"],
    ),
    # Finance / accounting.
    (
        re.compile(
            r"\b(finance(\s+(manager|director|analyst|partner|lead))?|"
            r"financial\s+(analyst|planner|advisor|controller)|"
            r"accountant|accounting(\s+(manager|lead|associate|clerk))?|"
            r"controller|"
            r"fp\s*&\s*a|fpa|"
            r"treasur(er|y(\s+(analyst|manager))?)|"
            r"audit(or)?(\s+(manager|associate|senior))?|"
            r"tax(\s+(manager|specialist|analyst|director))?|"
            r"bookkeep(er|ing))\b",
            re.IGNORECASE,
        ),
        ["accounting_and_finance"],
    ),
    # Legal / compliance.
    (
        re.compile(
            r"\b(legal(\s+(counsel|advisor|manager|associate|operations))?|"
            r"counsel|attorney|paralegal|"
            r"general\s+counsel|deputy\s+general\s+counsel|"
            r"compliance(\s+(officer|manager|analyst|specialist|lead))?|"
            r"privacy\s+(officer|counsel|manager)|"
            r"contracts?\s+(manager|specialist)|"
            r"regulatory\s+affairs)\b",
            re.IGNORECASE,
        ),
        ["legal_and_compliance"],
    ),
    # Operations / logistics / chief of staff / program / project management.
    (
        re.compile(
            r"\b(program\s+manager|project\s+manager|"
            r"technical\s+program\s+manager|tpm\s+(program)|"
            r"scrum\s+master|agile\s+coach|"
            r"chief\s+of\s+staff|"
            r"operations(\s+(manager|lead|director|associate|"
            r"analyst|specialist|coordinator))?|"
            r"\bops\b|business\s+operations|biz\s+ops|"
            r"strategy\s+(and|&)\s+operations|strat\s+ops|"
            r"supply\s+chain(\s+(manager|analyst))?|"
            r"logistics(\s+(coordinator|manager|specialist))?|"
            r"warehouse\s+(manager|associate)|"
            r"procurement(\s+(manager|specialist))?|"
            r"facilities(\s+(manager|coordinator))?)\b",
            re.IGNORECASE,
        ),
        ["management_and_executive"],
    ),
    # Consulting / strategy (analyst/associate/principal at consultancies).
    (
        re.compile(
            r"\b(consultant|consulting(\s+(associate|manager|director))?|"
            r"strategy\s+(consultant|associate|manager|director|lead)|"
            r"strategist|management\s+consultant|"
            r"transformation\s+(consultant|manager|lead))\b",
            re.IGNORECASE,
        ),
        ["consultant"],
    ),
    # Education / training.
    (
        re.compile(
            r"\b(teacher|instructor|professor|tutor|"
            r"curriculum\s+(developer|designer|manager)|"
            r"learning\s+designer|instructional\s+designer|"
            r"training(\s+(specialist|manager|coordinator))?|"
            r"trainer)\b",
            re.IGNORECASE,
        ),
        ["education_and_training"],
    ),
    # Public sector / government.
    (
        re.compile(
            r"\b(government\s+affairs|public\s+(policy|affairs|sector)|"
            r"policy\s+(analyst|advisor|manager|director)|"
            r"foreign\s+service|public\s+health(\s+(analyst|manager))?)\b",
            re.IGNORECASE,
        ),
        ["public_sector_and_government"],
    ),
    # Arts / entertainment / journalism.
    (
        re.compile(
            r"\b(journalist|reporter|editor(\s+(in|at))?|copy\s+editor|"
            r"producer|director\s+of\s+(film|video|content)|"
            r"videographer|photographer|"
            r"musician|composer|sound\s+(engineer|designer))\b",
            re.IGNORECASE,
        ),
        ["arts_and_entertainment"],
    ),
    # Business analyst (after data_analysis above so "data analyst" doesn't get this).
    (
        re.compile(
            r"\b(business\s+analyst|systems\s+analyst|"
            r"requirements\s+analyst)\b",
            re.IGNORECASE,
        ),
        ["business_analyst"],
    ),
    # Catch-all generic engineering — must come AFTER software/hardware so
    # specific patterns win.
    (
        re.compile(r"\b(engineer|engineering)\b", re.IGNORECASE),
        ["engineering_and_development"],
    ),
    # Generic management / leadership without function modifier.
    # Captures bare "VP", "Director", "Senior Manager" titles. Comes LAST so
    # function-specific patterns above win first (e.g. "Director of Marketing"
    # → marketing, "Sales Director" → sales).
    (
        re.compile(
            r"\b(vp|vice\s*president|"
            r"director|head\s+of|"
            r"manager|senior\s+manager|"
            r"executive)\b",
            re.IGNORECASE,
        ),
        ["management_and_executive"],
    ),
]


def infer_role_function(title: str) -> list[str]:
    """Classify a free-text job title to canonical jobright role-function tokens.

    Returns the first matching bucket as a single-element list, or ``[]`` if
    no pattern matches. Multi-pick is allowed by the schema (e.g. a
    "Sales Engineer" could be both ``sales`` and ``software_engineering``)
    but for v1.7 we keep it single-bucket and let the most-specific pattern
    win — multi-picks introduce noise into the V16 array-contains-any
    hard-filter.

    Empty list is intentional: it tells V16 ``queryMatchingJobs`` to drop
    the row because we can't confidently classify it. Better to skip than
    to mis-classify a janitor job as software_engineering.
    """
    if not title or not isinstance(title, str):
        return []
    for pattern, functions in ROLE_FUNCTION_REGEX:
        if pattern.search(title):
            return list(functions)
    return []
