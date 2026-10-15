import pytest

from leadforge.config import Settings
from leadforge.models import (
    Contact,
    EmailCandidate,
    VerificationMethod,
    VerificationVerdict,
)
from leadforge.scoring import evidence_tier, is_sendable, score


def candidate(**overrides) -> EmailCandidate:
    values = {
        "email": "jane.doe@example.com",
        "contact": Contact(first_name="Jane", last_name="Doe", domain="example.com"),
        "source": "page",
        "evidence_tier": "A",
    }
    values.update(overrides)
    return EmailCandidate(**values)


@pytest.mark.parametrize(
    ("overrides", "source_count", "matches", "expected"),
    [
        ({"verification_verdict": VerificationVerdict.SEND}, 1, False, 0.40),
        ({"found_on_web": True}, 1, False, 0.25),
        ({}, 2, False, 0.15),
        ({}, 1, True, 0.10),
        ({"catch_all_domain": True}, 1, False, 0.00),
        (
            {
                "verification_verdict": VerificationVerdict.SEND,
                "found_on_web": True,
                "catch_all_domain": True,
            },
            2,
            True,
            0.70,
        ),
    ],
)
def test_score_uses_spec_weights(overrides, source_count, matches, expected) -> None:
    assert (
        score(
            candidate(**overrides),
            source_count=source_count,
            pattern_matches_domain=matches,
        )
        == expected
    )


def test_unverified_pattern_only_is_capped_at_point_four() -> None:
    item = candidate(
        evidence_tier="C",
        source="pattern",
        verification_method=VerificationMethod.PATTERN_ONLY,
        verification_verdict=VerificationVerdict.UNKNOWN,
        found_on_web=True,
    )
    assert score(item, source_count=3, pattern_matches_domain=True) == 0.4


def test_tier_c_is_capped_and_non_sendable() -> None:
    item = candidate(
        evidence_tier="C",
        verification_verdict=VerificationVerdict.SEND,
    )
    item.confidence = score(item, source_count=3, pattern_matches_domain=True)
    settings = Settings(_env_file=None, min_confidence_to_emit=0.4)
    assert item.confidence == 0.4
    assert not is_sendable(item, settings)


def test_established_pattern_and_smtp_send_can_be_tier_b() -> None:
    item = candidate(
        evidence_tier="C",
        verification_method=VerificationMethod.SMTP,
        verification_verdict=VerificationVerdict.SEND,
        catch_all_domain=False,
    )
    assert evidence_tier(item, pattern_matches_domain=True) == "B"


def test_catch_all_without_web_evidence_is_never_sendable() -> None:
    item = candidate(
        evidence_tier="B",
        source="pattern",
        catch_all_domain=True,
        confidence=0.4,
    )
    settings = Settings(_env_file=None, min_confidence_to_emit=0.4)
    assert not is_sendable(item, settings)


def test_tier_a_catch_all_remains_sendable_if_other_rules_pass() -> None:
    item = candidate(
        evidence_tier="A",
        found_on_web=True,
        catch_all_domain=True,
        confidence=0.5,
        verification_verdict=VerificationVerdict.UNKNOWN,
    )
    settings = Settings(_env_file=None, min_confidence_to_emit=0.4)
    assert is_sendable(item, settings)


def test_killed_candidate_is_never_sendable() -> None:
    item = candidate(
        verification_verdict=VerificationVerdict.KILL,
        confidence=0.9,
    )
    assert not is_sendable(item, Settings(_env_file=None))
