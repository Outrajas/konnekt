"""Resume ingestion to an editable, reviewable operator profile."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pypdf import PdfReader

from leadforge.models import OperatorProfile

_SKILL_WORDS = {
    # Languages
    "python", "sql", "java", "javascript", "typescript", "go", "golang",
    "rust", "c++", "c#", "scala", "kotlin", "swift", "ruby", "php",
    # Web / backend
    "react", "vue", "angular", "node.js", "next.js", "django", "flask",
    "fastapi", "spring", "express", "rest api", "graphql",
    # Cloud / devops
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform",
    "github actions", "ci/cd", "jenkins",
    # Data
    "pandas", "numpy", "scipy", "postgresql", "mysql", "mongodb",
    "snowflake", "bigquery", "spark", "hadoop", "kafka", "airflow",
    "dbt", "excel", "tableau", "power bi", "looker",
    "data analysis", "data visualization",
    # ML / AI
    "machine learning", "deep learning", "pytorch", "tensorflow",
    "keras", "scikit-learn", "sklearn", "xgboost", "lightgbm",
    "transformers", "hugging face", "langchain", "llm", "llms",
    "rag", "vector database", "opencv", "nltk", "spacy",
    "computer vision", "nlp", "reinforcement learning",
    # General
    "git", "linux", "agile", "scrum",
}

def _compile_skill_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Compile word-boundary patterns for every skill.

    Uses negative lookaround instead of \b so that symbols like c++, c# and
    node.js still match at their real boundaries.
    """
    pats: list[tuple[str, re.Pattern[str]]] = []
    for skill in _SKILL_WORDS:
        esc = re.escape(skill)
        left = r"(?<![A-Za-z0-9])" if skill[0].isalnum() else ""
        right = r"(?![A-Za-z0-9])" if skill[-1].isalnum() else ""
        pats.append((skill, re.compile(left + esc + right)))
    return pats


_SKILL_PATTERNS = _compile_skill_patterns()


_TITLE_WORDS = (
    # Software
    "software engineer", "software developer",
    "backend engineer", "backend developer",
    "frontend engineer", "frontend developer",
    "full stack engineer", "full-stack engineer",
    "full stack developer", "full-stack developer",
    "web developer", "staff engineer", "principal engineer",
    "senior software engineer", "senior developer",
    # Data
    "data analyst", "business analyst", "data engineer",
    "data scientist", "analytics engineer", "bi analyst",
    "product analyst",
    # AI / ML
    "machine learning engineer", "ml engineer", "ai engineer",
    "artificial intelligence engineer", "deep learning engineer",
    "nlp engineer", "computer vision engineer",
    "research engineer", "research scientist",
    "applied scientist", "ai researcher", "ml researcher",
    # Other
    "developer", "programmer", "product manager", "engineering manager",
)

_TRACK_DEFAULT_TITLES: dict[str, list[str]] = {
    "ai_ml": [
        "machine learning engineer",
        "ml engineer",
        "ai engineer",
        "data scientist",
    ],
    "swe": [
        "software engineer",
        "software developer",
        "backend engineer",
        "full stack engineer",
    ],
    "data_analyst": [
        "data analyst",
        "business analyst",
        "analytics engineer",
    ],
}


def read_resume(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8-sig")
    raise ValueError("Resume must be a PDF, TXT, or Markdown file")


def extract_profile(text: str) -> OperatorProfile:
    normalized = re.sub(r"\s+", " ", text).lower()
    skills = sorted({skill for skill, pat in _SKILL_PATTERNS if pat.search(normalized)})
    titles: list[str] = []
    seen: set[str] = set()
    for t in _TITLE_WORDS:
        if t in normalized and t not in seen:
            seen.add(t)
            titles.append(t)
    return OperatorProfile(skills=skills, target_titles=titles)


def apply_track_defaults(profile: OperatorProfile, track: str) -> OperatorProfile:
    """Fill missing target_titles from the track's canonical defaults.

    Only fills if extraction produced nothing - otherwise extraction wins.
    """
    if profile.target_titles:
        return profile
    defaults = _TRACK_DEFAULT_TITLES.get(track)
    if not defaults:
        return profile
    return profile.model_copy(update={"target_titles": list(defaults)})


def save_profile(profile: OperatorProfile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )