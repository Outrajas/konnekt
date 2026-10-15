import pytest

from leadforge.db import connect, init_schema
from leadforge.models import Contact, EmailCandidate
from leadforge.stages.pattern_learn import is_established, record_pattern_sample
from leadforge.tools.email_patterns import generate, infer_pattern, rank_patterns


@pytest.mark.parametrize(
    ("first", "last", "expected_first", "expected_last"),
    [
        ("Élodie", "O'Neil-Smith", "elodie", "oneilsmith"),
        ("Dr. Jane", "Doe, Jr.", "jane", "doe"),
        ("Mary Jane", "van der Berg", "maryjane", "vanderberg"),
        ("José", "García II", "jose", "garcia"),
    ],
)
def test_generate_normalizes_name_parts(first, last, expected_first, expected_last) -> None:
    contact = Contact(first_name=first, last_name=last, domain="Example.com")
    actual = generate(contact)
    assert actual[0].email == f"{expected_first}.{expected_last}@example.com"
    assert all(item.source == "pattern" for item in actual)
    assert all(item.evidence_tier == "C" and item.confidence == 0.4 for item in actual)
    assert all(not item.found_on_web for item in actual)


def test_patterns_round_trip_many_contact_names() -> None:
    contacts = [
        Contact(first_name=first, last_name=last, domain="example.com")
        for first in ("Ada", "Jean-Luc", "Søren", "Mary Jane")
        for last in ("Lovelace", "O'Neil-Smith", "van der Berg")
    ]
    for contact in contacts:
        for candidate in generate(contact):
            assert infer_pattern(contact, candidate.email) == candidate.pattern


def test_generate_uses_requested_pattern_order_and_rejects_unknown() -> None:
    contact = Contact(first_name="Jane", last_name="Doe", domain="example.com")
    assert [item.pattern for item in generate(contact, ["last", "first"])] == ["last", "first"]
    with pytest.raises(ValueError, match="Unknown email pattern"):
        generate(contact, ["invented"])


def test_rank_patterns_uses_a_learned_domain_pattern(tmp_path) -> None:
    conn = connect(tmp_path / "patterns.db")
    try:
        init_schema(conn)
        conn.execute(
            "INSERT INTO domain_pattern(domain,pattern,sample_count,last_checked) "
            "VALUES('example.com','flast',1,'2026-01-01T00:00:00+00:00')"
        )
        assert rank_patterns("EXAMPLE.COM", conn)[0] == "flast"
    finally:
        conn.close()


def test_learning_requires_tier_a_and_two_matching_samples(tmp_path) -> None:
    conn = connect(tmp_path / "learn.db")
    try:
        init_schema(conn)
        contact = Contact(first_name="Jane", last_name="Doe", domain="example.com")
        pattern_only = EmailCandidate(
            email="jdoe@example.com", contact=contact, source="pattern", evidence_tier="C"
        )
        assert not record_pattern_sample(conn, contact, pattern_only)
        assert not is_established(conn, "example.com")

        first = EmailCandidate(
            email="jdoe@example.com", contact=contact, source="team page", evidence_tier="A"
        )
        second_contact = Contact(first_name="John", last_name="Smith", domain="example.com")
        second = EmailCandidate(
            email="jsmith@example.com",
            contact=second_contact,
            source="team page",
            evidence_tier="A",
        )
        assert record_pattern_sample(conn, contact, first)
        assert not is_established(conn, "example.com")
        assert record_pattern_sample(conn, second_contact, second)
        assert is_established(conn, "example.com")
        row = conn.execute("SELECT pattern,sample_count FROM domain_pattern").fetchone()
        assert (row["pattern"], row["sample_count"]) == ("flast", 2)
        assert not record_pattern_sample(conn, contact, first)
    finally:
        conn.close()
