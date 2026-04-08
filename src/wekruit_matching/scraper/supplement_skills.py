"""Supplement skills and industry for JobRight GitHub jobs that have no skills data.

Two strategies:
1. Title-based skill extraction — keyword matching on role_title for ALL jobs
2. Better industry inference — use role_title keywords to reclassify "other" industry

Run: uv run python -m wekruit_matching.scraper.supplement_skills
"""
from __future__ import annotations

import re

import psycopg
from loguru import logger

from wekruit_matching.db.connection import get_connection

# Skills dictionary — extract from job title keywords
TITLE_SKILL_MAP: dict[str, list[str]] = {
    # Software Engineering
    r"\bsoftware\b": ["software engineering"],
    r"\bfull[- ]?stack\b": ["javascript", "react", "node.js", "sql"],
    r"\bfrontend\b|\bfront[- ]?end\b": ["javascript", "react", "css", "html"],
    r"\bbackend\b|\bback[- ]?end\b": ["python", "sql", "api"],
    r"\bweb\s+dev": ["javascript", "html", "css"],
    r"\bios\b": ["swift", "ios"],
    r"\bandroid\b": ["kotlin", "android"],
    r"\bmobile\b": ["mobile development"],
    # Data / ML / AI
    r"\bdata\s+scien": ["python", "machine learning", "sql", "pandas"],
    r"\bdata\s+analy": ["sql", "python", "excel", "tableau"],
    r"\bdata\s+engineer": ["python", "sql", "aws", "spark"],
    r"\bmachine\s+learning\b|\bml\b": ["python", "machine learning", "pytorch"],
    r"\bai\b|\bartificial\s+intelligence\b": ["python", "machine learning", "deep learning"],
    r"\bnlp\b|\bnatural\s+language\b": ["python", "nlp", "machine learning"],
    r"\bcomputer\s+vision\b": ["python", "computer vision", "pytorch"],
    r"\bdeep\s+learning\b": ["python", "deep learning", "pytorch", "tensorflow"],
    # Cloud / DevOps / Infra
    r"\bdevops\b": ["docker", "kubernetes", "aws", "terraform", "linux"],
    r"\bcloud\b": ["aws", "gcp", "azure"],
    r"\bsre\b|\breliability\b": ["linux", "kubernetes", "monitoring"],
    r"\bplatform\s+engineer": ["kubernetes", "docker", "terraform", "aws"],
    r"\binfrastructure\b": ["aws", "terraform", "linux"],
    # Security
    r"\bcyber\s*security\b|\bsecurity\s+engineer\b": ["cybersecurity", "linux", "networking"],
    r"\bpenetration\b|\bpentest\b": ["cybersecurity", "linux"],
    # Product / Design / Business
    r"\bproduct\s+manag": ["product management", "agile", "jira"],
    r"\bux\b|\bui\b|\bdesign": ["figma", "design"],
    r"\bbusiness\s+analy": ["sql", "excel", "data analysis"],
    r"\bconsult": ["excel", "powerpoint", "data analysis"],
    r"\bfinance\b|\bfinancial\b": ["excel", "financial modeling", "sql"],
    r"\bmarketing\b": ["marketing", "excel", "analytics"],
    r"\baccounting\b": ["excel", "accounting"],
    # Specific tech stacks in titles
    r"\bpython\b": ["python"],
    r"\bjava\b(?!script)": ["java"],
    r"\breact\b": ["react", "javascript"],
    r"\bnode\.?js\b": ["node.js", "javascript"],
    r"\baws\b": ["aws"],
    r"\bc\+\+\b": ["c++"],
    r"\bgolang\b|\b(?:^|\s)go\s+(?:developer|engineer)\b": ["go"],
    r"\brust\b": ["rust"],
    r"\bkubernetes\b|\bk8s\b": ["kubernetes", "docker"],
    r"\bsql\b": ["sql"],
}

# Industry inference from title keywords
TITLE_INDUSTRY_MAP: dict[str, str] = {
    r"\bsoftware\b|\bfrontend\b|\bbackend\b|\bfull[- ]?stack\b|\bweb\s+dev": "tech",
    r"\bdata\s+scien|\bml\b|\bmachine\s+learn|\bai\b|\bdeep\s+learn|\bnlp\b": "ai_ml",
    r"\bdevops\b|\bcloud\b|\bsre\b|\bplatform\b|\binfrastructure\b": "tech",
    r"\bcyber\s*security\b|\bsecurity\s+engineer\b|\bpentest": "cybersecurity",
    r"\bfinance\b|\bfinancial\b|\baccounting\b|\bbanking\b": "fintech",
    r"\bhealthcare\b|\bmedical\b|\bbiotech\b|\bpharma": "healthtech",
    r"\bmarketing\b": "other",
    r"\bconsult": "consulting",
    r"\bproduct\s+manag": "tech",
    r"\bhardware\b|\belectrical\b|\bmechanical\b|\bcivil\b": "hardware",
    r"\bgaming\b|\bgame\s+dev": "gaming",
    r"\becommerce\b|\bretail\b": "ecommerce",
}


def extract_skills_from_title(title: str) -> list[str]:
    """Extract skills from a job title using keyword patterns."""
    title_lower = title.lower()
    skills: set[str] = set()

    for pattern, skill_list in TITLE_SKILL_MAP.items():
        if re.search(pattern, title_lower):
            skills.update(skill_list)

    return sorted(skills)[:10]  # Cap at 10


def infer_industry_from_title(title: str) -> str | None:
    """Infer industry from job title keywords."""
    title_lower = title.lower()

    for pattern, industry in TITLE_INDUSTRY_MAP.items():
        if re.search(pattern, title_lower):
            return industry

    return None


def supplement_skills(conn: psycopg.Connection) -> dict[str, int]:
    """Supplement skills and industry for jobs that have none.

    Only updates jobs where required_skills is empty/null AND industry is 'other'.
    Does NOT overwrite existing enrichment data.
    """
    # Fetch jobs needing supplementation
    rows = conn.execute(
        """
        SELECT job_id, role_title, industry, required_skills
        FROM jobs
        WHERE status = 'active'
          AND (
            required_skills IS NULL
            OR required_skills = '{}'
            OR industry = 'other'
          )
        """
    ).fetchall()

    logger.info("Found {} jobs needing skill/industry supplementation", len(rows))

    skills_updated = 0
    industry_updated = 0

    for row in rows:
        job_id = row["job_id"]
        title = row["role_title"]
        current_skills = row["required_skills"] or []
        current_industry = row["industry"]

        updates = {}

        # Supplement skills if empty
        if not current_skills:
            new_skills = extract_skills_from_title(title)
            if new_skills:
                updates["required_skills"] = new_skills
                skills_updated += 1

        # Supplement industry if "other"
        if current_industry == "other":
            new_industry = infer_industry_from_title(title)
            if new_industry:
                updates["industry"] = new_industry
                industry_updated += 1

        if updates:
            set_clauses = []
            params: dict = {"job_id": job_id}
            if "required_skills" in updates:
                set_clauses.append("required_skills = %(skills)s")
                params["skills"] = updates["required_skills"]
            if "industry" in updates:
                set_clauses.append("industry = %(industry)s")
                params["industry"] = updates["industry"]

            conn.execute(
                f"UPDATE jobs SET {', '.join(set_clauses)} WHERE job_id = %(job_id)s",
                params,
            )

    conn.commit()
    logger.info(
        "Supplementation complete: {} skills updated, {} industry updated",
        skills_updated, industry_updated,
    )
    return {"skills_updated": skills_updated, "industry_updated": industry_updated}


if __name__ == "__main__":
    import sys
    logger.remove()
    logger.add(sys.stderr, level="INFO")

    logger.info("Starting skill/industry supplementation")
    with get_connection() as conn:
        stats = supplement_skills(conn)
    logger.info("Done. Stats: {}", stats)
