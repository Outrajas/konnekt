"""Operator identity, draft signature, and sending-domain readiness."""

from __future__ import annotations

from leadforge import email_identity
from leadforge.config import Settings
from leadforge.email_identity import _guidance, check_domain, sending_address, signature


def test_signature_is_empty_without_identity() -> None:
    assert signature(Settings(_env_file=None)) == ""


def test_signature_uses_name_and_email() -> None:
    settings = Settings(
        _env_file=None, operator_name="Ojas", operator_email="ojas@outrajas.tech"
    )
    assert signature(settings) == "\n\n--\nOjas\nojas@outrajas.tech"


def test_signature_tolerates_name_only() -> None:
    assert signature(Settings(_env_file=None, operator_name="Ojas")) == "\n\n--\nOjas"


def test_sending_address_prefers_operator_email() -> None:
    settings = Settings(
        _env_file=None,
        operator_email="ojas@outrajas.tech",
        smtp_mail_from="verify@outrajas.tech",
    )
    assert sending_address(settings) == "ojas@outrajas.tech"


def test_sending_address_falls_back_to_mail_from() -> None:
    settings = Settings(_env_file=None, smtp_mail_from="verify@outrajas.tech")
    assert sending_address(settings) == "verify@outrajas.tech"


def test_guidance_covers_every_missing_record() -> None:
    tips = _guidance("outrajas.tech", has_mx=False, has_spf=False, has_dmarc=False)
    assert any("MX" in tip for tip in tips)
    assert any("v=spf1" in tip for tip in tips)
    assert any("_dmarc" in tip for tip in tips)


def test_guidance_is_empty_when_everything_exists() -> None:
    assert _guidance("outrajas.tech", has_mx=True, has_spf=True, has_dmarc=True) == []


def test_check_domain_reports_records_without_network(monkeypatch) -> None:
    monkeypatch.setattr(email_identity, "_txt", lambda name: ["v=spf1 -all"]
                        if name == "outrajas.tech" else [])
    monkeypatch.setattr(
        email_identity, "_records",
        lambda name, kind: ["10 mx.example.net."] if kind == "MX" else ["203.0.113.9"],
    )
    result = check_domain("outrajas.tech")
    assert result["mx"] == ["10 mx.example.net."]
    assert result["spf"] == ["v=spf1 -all"]
    assert result["can_send"] is True
    assert result["can_receive"] is True
    assert result["guidance"]  # DMARC still missing


def test_check_domain_marks_a_parked_domain_as_not_ready(monkeypatch) -> None:
    monkeypatch.setattr(email_identity, "_txt", lambda name: [])
    monkeypatch.setattr(
        email_identity,
        "_records",
        lambda name, kind: ["91.195.240.94"] if kind == "A" else [],
    )
    result = check_domain("outrajas.tech")
    assert result["a"] == ["91.195.240.94"]
    assert result["can_send"] is False
    assert result["can_receive"] is False
    assert len(result["guidance"]) >= 3
