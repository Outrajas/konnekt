"""Explainable deterministic job/profile matching."""

from __future__ import annotations

import re

from leadforge.models import Job, OperatorProfile


def match_job(job: Job, profile: OperatorProfile) -> Job:
    title_tokens = _tokens(job.title)
    description_tokens = _tokens(job.description)
    target = {_token for value in profile.skills for _token in _tokens(value)}
    adjacent = {_token for value in profile.adjacent_skills for _token in _tokens(value)}
    target_titles = [_tokens(value) for value in profile.target_titles]
    adjacent_titles = [_tokens(value) for value in profile.adjacent_titles]
    matched_skills = sorted(target & (title_tokens | description_tokens))
    matched_adjacent = sorted(adjacent & (title_tokens | description_tokens))
    title_bonus = any(tokens and tokens <= title_tokens for tokens in target_titles)
    adjacent_bonus = any(tokens and tokens <= title_tokens for tokens in adjacent_titles)
    score = min(
        1.0,
        0.6 * len(matched_skills) / max(1, len(target))
        + 0.2 * len(matched_adjacent) / max(1, len(adjacent))
        + (0.2 if title_bonus else 0.1 if adjacent_bonus else 0.0),
    )
    reasons = [f"skill:{token}" for token in matched_skills]
    reasons += [f"adjacent-skill:{token}" for token in matched_adjacent]
    if title_bonus:
        reasons.append("target title match")
    elif adjacent_bonus:
        reasons.append("adjacent title match")
    return job.model_copy(update={"match_score": score, "match_reasons": reasons})


def _tokens(value: str) -> set[str]:
    return {word.lower() for word in re.findall(r"[A-Za-z0-9+#.]+", value) if len(word) > 1}
