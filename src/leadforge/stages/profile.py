"""Resume ingestion to an editable, reviewable operator profile."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pypdf import PdfReader

from leadforge.models import OperatorProfile

_SKILL_WORDS = {
    "python",
    "sql",
    "java",
    "javascript",
    "typescript",
    "react",
    "node.js",
    "fastapi",
    "django",
    "flask",
    "aws",
    "azure",
    "docker",
    "kubernetes",
    "machine learning",
    "data analysis",
    "pandas",
    "excel",
    "git",
    "linux",
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
    skills = sorted(skill for skill in _SKILL_WORDS if skill in normalized)
    titles = [
        title
        for title in (
            "software engineer",
            "data analyst",
            "data engineer",
            "product manager",
            "business analyst",
            "developer",
        )
        if title in normalized
    ]
    return OperatorProfile(skills=skills, target_titles=titles)


def save_profile(profile: OperatorProfile, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(profile.model_dump(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
