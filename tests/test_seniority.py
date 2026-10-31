"""Tests for seniority inference."""
from __future__ import annotations

import pytest

from leadforge.seniority import infer_seniority


@pytest.mark.parametrize("title,expected", [
    ("Head of Engineering", "exec"),
    ("Director of Product", "exec"),
    ("VP of Engineering", "exec"),
    ("CTO", "exec"),
    ("Principal Engineer", "principal"),
    ("Distinguished Architect", "principal"),
    ("Staff Engineer", "staff"),
    ("Tech Lead", "lead"),
    ("Team Lead Engineer", "lead"),
    ("Lead Software Engineer", "lead"),
    ("Senior Software Engineer", "senior"),
    ("Sr. Backend Engineer", "senior"),
    ("Sr Backend Engineer", "senior"),
    ("Engineer III", "senior"),
    ("SDE III", "senior"),
    ("SDE 3", "senior"),
    ("Software Engineer II", ""),  # "II" alone isn't decisive
    ("ML Intern", "intern"),
    ("Software Engineer Intern", "intern"),
    ("Junior Developer", "entry"),
    ("Jr. Software Engineer", "entry"),
    ("Entry Level Engineer", "entry"),
    ("Fresher", "entry"),
    ("New Grad Engineer", "entry"),
    ("Software Engineer", ""),  # no marker
    ("Machine Learning Engineer", ""),  # no marker
])
def test_title_inference(title, expected):
    result = infer_seniority(title, "")
    if expected == "":
        assert result == "unknown"
    else:
        assert result == expected, f"{title!r} -> {result!r} (want {expected!r})"


def test_description_years_0_to_2():
    assert infer_seniority("Engineer", "0-2 years experience") == "entry"


def test_description_one_plus():
    assert infer_seniority("Engineer", "1+ years experience required") == "entry"


def test_description_five_plus():
    assert infer_seniority("Engineer", "5+ years required") == "senior"


def test_description_eight_plus():
    assert infer_seniority("Engineer", "8+ years experience") == "staff"


def test_description_3_to_5_years():
    assert infer_seniority("Engineer", "3-5 years of experience") == "mid"


def test_title_beats_description():
    # "Senior" title + "0-2 years" description → still senior
    assert infer_seniority("Senior Engineer", "0-2 years") == "senior"


def test_description_fills_gap_when_title_silent():
    assert infer_seniority("Engineer", "8+ years experience required") == "staff"