from leadforge import cli
from leadforge.draft_quality import quality_issues
from leadforge.models import Contact


def test_quality_checker_accepts_complete_draft_and_rejects_bad_content() -> None:
    contact = Contact(
        first_name="Jane", last_name="Doe", domain="example.invalid", company="Example"
    )
    draft = cli._compose_draft(
        contact,
        "jane@example.invalid",
        (
            "engineering update",
            "published release notes for the onboarding API",
            "https://example.invalid/blog",
        ),
    )
    assert quality_issues(draft) == []
    assert len(draft.splitlines()) > 0
    assert quality_issues("NEEDS_MANUAL_PERSONALIZATION\n") == ["NEEDS_MANUAL_PERSONALIZATION"]
    invalid = draft.replace("Would you be open", "Amazing work! Would you be open")
    assert "contains banned flattery" in quality_issues(invalid)


def test_draft_without_professional_evidence_requires_manual_personalization() -> None:
    contact = Contact(first_name="Jane", last_name="Doe", domain="example.invalid")
    draft = cli._compose_draft(contact, "jane@example.invalid", None)
    assert draft == "NEEDS_MANUAL_PERSONALIZATION\n"
    assert "NEEDS_MANUAL_PERSONALIZATION" in quality_issues(draft)
