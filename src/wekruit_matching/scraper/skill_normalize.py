"""Skill name normalization for JobRight enrichment pipeline.

Maps verbose skill names from external sources (primarily JobRight) to our
internal lowercase vocabulary so that downstream matching is consistent.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Canonical mapping: external name -> internal vocabulary
# ---------------------------------------------------------------------------

SKILL_NORMALIZE: dict[str, str] = {
    # Cloud
    "Amazon Web Services": "aws",
    "Google Cloud": "gcp",
    "Google Cloud Platform": "gcp",
    "Microsoft Azure": "azure",
    # Languages
    "JavaScript": "javascript",
    "TypeScript": "typescript",
    "Python": "python",
    "Java": "java",
    "C++": "c++",
    "C#": "c#",
    "Golang": "go",
    "Go": "go",
    "Ruby": "ruby",
    "Rust": "rust",
    "Swift": "swift",
    "Kotlin": "kotlin",
    "Scala": "scala",
    "R": "r",
    "MATLAB": "matlab",
    "Perl": "perl",
    # Frameworks
    "React.js": "react",
    "ReactJS": "react",
    "React": "react",
    "Node.js": "node.js",
    "NodeJS": "node.js",
    "Angular": "angular",
    "Vue.js": "vue",
    "VueJS": "vue",
    "Django": "django",
    "Flask": "flask",
    "Spring Boot": "spring boot",
    "Spring": "spring",
    "Express.js": "express",
    "Next.js": "next.js",
    "FastAPI": "fastapi",
    "ASP.NET": "asp.net",
    "Ruby on Rails": "ruby on rails",
    "Laravel": "laravel",
    # Databases
    "PostgreSQL": "postgresql",
    "MySQL": "mysql",
    "MongoDB": "mongodb",
    "Redis": "redis",
    "Elasticsearch": "elasticsearch",
    "DynamoDB": "dynamodb",
    "Cassandra": "cassandra",
    "SQLite": "sqlite",
    "Oracle": "oracle",
    "SQL Server": "sql server",
    # DevOps
    "Docker": "docker",
    "Kubernetes": "kubernetes",
    "Terraform": "terraform",
    "Ansible": "ansible",
    "Jenkins": "jenkins",
    "GitHub Actions": "github actions",
    "CircleCI": "circleci",
    "GitLab CI": "gitlab ci",
    # Data / ML
    "Apache Spark": "spark",
    "Apache Kafka": "kafka",
    "Apache Airflow": "airflow",
    "Hadoop": "hadoop",
    "Snowflake": "snowflake",
    "Databricks": "databricks",
    "DBT": "dbt",
    "Pandas": "pandas",
    "NumPy": "numpy",
    "Scikit-learn": "scikit-learn",
    "TensorFlow": "tensorflow",
    "PyTorch": "pytorch",
    # General / methodology
    "Machine Learning": "machine learning",
    "Deep Learning": "deep learning",
    "Natural Language Processing": "nlp",
    "NLP": "nlp",
    "Computer Vision": "computer vision",
    "Data Structures": "data structures",
    "Algorithms": "algorithms",
    "RESTful APIs": "rest",
    "REST": "rest",
    "GraphQL": "graphql",
    "gRPC": "grpc",
    "CI/CD": "ci/cd",
    "Agile": "agile",
    "Scrum": "scrum",
    "Git": "git",
    "Linux": "linux",
    "SQL": "sql",
    "HTML": "html",
    "CSS": "css",
    # Non-tech / domain
    "Salesforce": "salesforce",
    "SAP": "sap",
    "Tableau": "tableau",
    "Power BI": "power bi",
    "Excel": "excel",
    "Figma": "figma",
    "Adobe Creative Suite": "adobe creative suite",
    "AutoCAD": "autocad",
    "SolidWorks": "solidworks",
    "GAAP": "accounting",
    "IFRS": "accounting",
    "Six Sigma": "six sigma",
    "Lean": "lean",
    "PMP": "pmp",
    "CPA": "cpa",
}

# Pre-compute a case-insensitive lookup table once at import time.
_LOOKUP: dict[str, str] = {k.lower(): v for k, v in SKILL_NORMALIZE.items()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def normalize_skill(raw: str) -> str:
    """Return the canonical internal name for *raw*.

    Lookup is case-insensitive.  Unknown skills are returned as-is in
    lowercase with surrounding whitespace stripped.
    """
    stripped = raw.strip()
    return _LOOKUP.get(stripped.lower(), stripped.lower())


def normalize_skills(skills: list[str]) -> list[str]:
    """Normalize every skill in *skills*, deduplicate, and preserve order."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in skills:
        normed = normalize_skill(raw)
        if normed not in seen:
            seen.add(normed)
            out.append(normed)
    return out


# ---------------------------------------------------------------------------
# Quick smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Single-skill normalization
    assert normalize_skill("Amazon Web Services") == "aws"
    assert normalize_skill("  Google Cloud Platform ") == "gcp"
    assert normalize_skill("React.js") == "react"
    assert normalize_skill("reactjs") == "react"  # case-insensitive
    assert normalize_skill("NLP") == "nlp"
    assert normalize_skill("Natural Language Processing") == "nlp"
    assert normalize_skill("GAAP") == "accounting"
    assert normalize_skill("IFRS") == "accounting"
    assert normalize_skill("Unknown Skill XYZ") == "unknown skill xyz"

    # Batch normalization with dedup
    result = normalize_skills([
        "React.js",
        "ReactJS",
        "React",
        "Node.js",
        "NodeJS",
        "Python",
        "python",
        "  Python ",
        "Some New Skill",
    ])
    assert result == ["react", "node.js", "python", "some new skill"], f"Got: {result}"

    # Empty input
    assert normalize_skills([]) == []

    print("All tests passed.")
