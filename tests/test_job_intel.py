"""Tests for job_intel extraction (salary + tech stack)."""
from __future__ import annotations

from leadforge.job_intel import extract_salary, extract_tech_stack

# ---------------- salary ----------------


def test_salary_usd_full_range():
    s = extract_salary("$120,000 - $180,000 per year")
    assert s is not None
    assert s.currency == "USD"
    assert s.min_amount == 120_000
    assert s.max_amount == 180_000
    assert s.period == "year"


def test_salary_usd_short_form():
    s = extract_salary("Compensation: $120k-$180k annually")
    assert s is not None
    assert s.currency == "USD"
    assert s.min_amount == 120_000
    assert s.max_amount == 180_000
    assert s.period == "year"


def test_salary_inr_lpa():
    s = extract_salary("₹12-15 LPA")
    assert s is not None
    assert s.currency == "INR"
    assert s.min_amount == 1_200_000
    assert s.max_amount == 1_500_000
    assert s.period == "year"


def test_salary_inr_lpa_with_decimals():
    s = extract_salary("INR 12.5-15.5 LPA")
    assert s is not None
    assert s.currency == "INR"
    assert s.min_amount == 1_250_000
    assert s.max_amount == 1_550_000


def test_salary_gbp_range():
    s = extract_salary("£70k - £90k per annum")
    assert s is not None
    assert s.currency == "GBP"
    assert s.min_amount == 70_000
    assert s.max_amount == 90_000


def test_salary_hourly():
    s = extract_salary("$50 - $65 per hour")
    assert s is not None
    assert s.currency == "USD"
    assert s.period == "hour"
    assert s.min_amount == 50
    assert s.max_amount == 65


def test_salary_single_value():
    s = extract_salary("Salary: $120,000")
    assert s is not None
    assert s.currency == "USD"
    assert s.min_amount == 120_000
    assert s.max_amount == 120_000


def test_salary_single_k():
    s = extract_salary("up to $150k depending on experience")
    assert s is not None
    assert s.min_amount == 150_000
    assert s.max_amount == 150_000


def test_salary_missing_competitive():
    assert extract_salary("Competitive salary, DOE.") is None


def test_salary_missing_prose():
    assert extract_salary("We offer great benefits and a collaborative culture.") is None


def test_salary_empty():
    assert extract_salary("") is None


# ---------------- tech stack ----------------


def test_tech_basic():
    t = extract_tech_stack("We use Python, SQL, and AWS.")
    for s in ("python", "sql", "aws"):
        assert s in t


def test_tech_from_title():
    t = extract_tech_stack("", "Senior ML Engineer - PyTorch")
    assert "pytorch" in t


def test_tech_word_boundary_no_r():
    t = extract_tech_stack("3 years of experience required")
    assert "r" not in t


def test_tech_word_boundary_no_go_in_algorithms():
    t = extract_tech_stack("Designed efficient algorithms for sorting")
    assert "go" not in t
    assert "golang" not in t


def test_tech_dedup():
    t = extract_tech_stack("Python, python, PYTHON everywhere")
    assert t.count("python") == 1


def test_tech_extras_kubernetes():
    t = extract_tech_stack("Deploy with Kubernetes, Helm, and ArgoCD.")
    assert "kubernetes" in t
    assert "helm" in t
    assert "argocd" in t


def test_tech_extras_k8s_short_form():
    t = extract_tech_stack("Experience with k8s required.")
    assert "k8s" in t


def test_tech_extras_mlops():
    t = extract_tech_stack("Use MLflow and Kubeflow for the pipeline")
    assert "mlflow" in t
    assert "kubeflow" in t


def test_tech_combined():
    t = extract_tech_stack(
        "Looking for Python, PyTorch, AWS. Nice to have: Terraform, k8s.",
        "Senior AI Engineer",
    )
    for s in ("python", "pytorch", "aws", "terraform", "k8s"):
        assert s in t


def test_tech_empty():
    assert extract_tech_stack("") == []
    assert extract_tech_stack("", "") == []