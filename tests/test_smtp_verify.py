import asyncio
import socket

import dns.exception
import pytest
from aiosmtpd.controller import Controller

from leadforge.config import Settings
from leadforge.models import (
    Contact,
    EmailCandidate,
    VerificationMethod,
    VerificationVerdict,
)
from leadforge.tools.smtp_verify import (
    Port25Status,
    Probe,
    Unreachable,
    check_port25_status,
    classify,
    is_valid_syntax,
    resolve_mx,
    verify_candidates,
)


def make_contact(domain: str = "example.test") -> Contact:
    return Contact(first_name="Jane", last_name="Doe", domain=domain)


def make_candidate(email: str, confidence: float = 0.9) -> EmailCandidate:
    return EmailCandidate(
        email=email,
        contact=make_contact(email.rsplit("@", 1)[-1]),
        source="test",
        confidence=confidence,
    )


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "smtp_mail_from": "verify@example.test",
        "smtp_timeout_seconds": 2.0,
        "smtp_max_concurrent": 3,
        "request_delay_seconds": 0.0,
    }
    values.update(overrides)
    return Settings(**values)


def test_syntax() -> None:
    assert is_valid_syntax("jane.doe@example.com")
    assert not is_valid_syntax("jane@@example.com")
    assert not is_valid_syntax("jane@localhost")
    assert not is_valid_syntax("")


def test_classify() -> None:
    assert classify(250) == "send"
    assert classify(251) == "send"
    assert classify(550, "5.1.1 user unknown") == "kill"
    assert classify(550, "blocked by Spamhaus") == "unknown"
    assert classify(550, "5.7.1 policy blocklist rejection") == "unknown"
    assert classify(451, "greylisted") == "hold"
    assert classify(554) == "unknown"


@pytest.mark.asyncio
async def test_no_mx_kills_candidates_without_probing() -> None:
    async def no_mx(domain: str) -> list[str]:
        assert domain == "example.test"
        return []

    async def unexpected_probe(*args: object) -> Probe:
        raise AssertionError("probe must not run when no MX record exists")

    candidates = [make_candidate("jane@example.test"), make_candidate("j.doe@example.test")]
    results = await verify_candidates(
        make_contact(), candidates, make_settings(), mx_resolver=no_mx, probe_fn=unexpected_probe
    )

    assert [result.verification_verdict for result in results] == [
        VerificationVerdict.KILL,
        VerificationVerdict.KILL,
    ]


@pytest.mark.asyncio
async def test_dns_timeout_never_kills_candidate() -> None:
    async def timed_out(domain: str) -> list[str]:
        raise dns.exception.Timeout("DNS timed out")

    [result] = await verify_candidates(
        make_contact(),
        [make_candidate("jane@example.test", confidence=0.95)],
        make_settings(),
        mx_resolver=timed_out,
    )

    assert result.verification_verdict is VerificationVerdict.UNKNOWN
    assert result.verification_method is VerificationMethod.PATTERN_ONLY
    assert result.confidence == 0.4
    assert "dns error" in result.notes


@pytest.mark.asyncio
async def test_other_resolver_errors_are_unknown_pattern_only() -> None:
    async def resolver_failure(domain: str) -> list[str]:
        raise RuntimeError("resolver configuration is broken")

    [result] = await verify_candidates(
        make_contact(),
        [make_candidate("jane@example.test")],
        make_settings(),
        mx_resolver=resolver_failure,
    )

    assert result.verification_verdict is VerificationVerdict.UNKNOWN
    assert result.verification_method is VerificationMethod.PATTERN_ONLY
    assert result.confidence == 0.4
    assert "dns error" in result.notes


@pytest.mark.asyncio
async def test_resolve_mx_propagates_nameserver_and_timeout_errors(monkeypatch) -> None:
    async def fail(*args, **kwargs):
        raise dns.resolver.NoNameservers

    class FakeResolver:
        async def resolve(self, *args, **kwargs):
            return await fail(*args, **kwargs)

    monkeypatch.setattr(
        "leadforge.tools.smtp_verify.dns.asyncresolver.Resolver", lambda **kwargs: FakeResolver()
    )
    with pytest.raises(dns.resolver.NoNameservers):
        await resolve_mx("example.test")

    async def timeout(*args, **kwargs):
        raise dns.exception.Timeout("timed out")

    class TimeoutResolver:
        async def resolve(self, *args, **kwargs):
            return await timeout(*args, **kwargs)

    monkeypatch.setattr(
        "leadforge.tools.smtp_verify.dns.asyncresolver.Resolver", lambda **kwargs: TimeoutResolver()
    )
    with pytest.raises(dns.exception.Timeout):
        await resolve_mx("example.test")


@pytest.mark.asyncio
async def test_resolve_mx_nxdomain_and_null_mx_are_negative_answers(monkeypatch) -> None:
    async def nxdomain(*args, **kwargs):
        raise dns.resolver.NXDOMAIN

    class NXResolver:
        async def resolve(self, *args, **kwargs):
            return await nxdomain(*args, **kwargs)

    monkeypatch.setattr(
        "leadforge.tools.smtp_verify.dns.asyncresolver.Resolver", lambda **kwargs: NXResolver()
    )
    assert await resolve_mx("missing.example") == []

    class NullRecord:
        preference = 0
        exchange = "."

    async def null_mx(*args, **kwargs):
        return [NullRecord()]

    class NullResolver:
        async def resolve(self, *args, **kwargs):
            return await null_mx(*args, **kwargs)

    monkeypatch.setattr(
        "leadforge.tools.smtp_verify.dns.asyncresolver.Resolver", lambda **kwargs: NullResolver()
    )
    assert await resolve_mx("nomail.example") == []


@pytest.mark.asyncio
async def test_resolve_mx_uses_explicit_nameservers(monkeypatch) -> None:
    class Record:
        preference = 5
        exchange = "mx.example.test."

    class AnswerResolver:
        nameservers = []

        async def resolve(self, *args, **kwargs):
            return [Record()]

    resolver = AnswerResolver()
    monkeypatch.setattr(
        "leadforge.tools.smtp_verify.dns.asyncresolver.Resolver", lambda **kwargs: resolver
    )
    assert await resolve_mx("example.test", nameservers=["1.1.1.1"]) == [
        "mx.example.test"
    ]
    assert resolver.nameservers == ["1.1.1.1"]


@pytest.mark.asyncio
async def test_unreachable_mx_uses_pattern_only_and_caps_confidence() -> None:
    async def mx(domain: str) -> list[str]:
        return ["mx.example.test"]

    async def unreachable(*args: object) -> Probe:
        raise Unreachable("connection refused")

    candidate = make_candidate("jane@example.test", confidence=0.9)
    [result] = await verify_candidates(
        make_contact(), [candidate], make_settings(), mx_resolver=mx, probe_fn=unreachable
    )

    assert result.verification_verdict is VerificationVerdict.UNKNOWN
    assert result.verification_method is VerificationMethod.PATTERN_ONLY
    assert result.confidence == 0.4


@pytest.mark.asyncio
async def test_catch_all_domain_is_flagged_and_confidence_is_capped() -> None:
    async def mx(domain: str) -> list[str]:
        return ["mx.example.test"]

    async def accept_all(*args: object) -> Probe:
        return Probe(250, "accepted", 25, "mx.example.test")

    [result] = await verify_candidates(
        make_contact(),
        [make_candidate("jane@example.test", confidence=0.95)],
        make_settings(),
        mx_resolver=mx,
        probe_fn=accept_all,
    )

    assert result.catch_all_domain is True
    assert result.verification_verdict is VerificationVerdict.SEND
    assert result.confidence == 0.4


@pytest.mark.asyncio
async def test_invalid_syntax_is_killed_and_temporary_reply_is_held() -> None:
    async def mx(domain: str) -> list[str]:
        return ["mx.example.test"]

    async def probe(hosts: list[str], address: str, *args: object) -> Probe:
        if address.startswith("jane-"):
            return Probe(550, "5.1.1 user unknown", 25, hosts[0])
        return Probe(451, "4.7.1 try again later", 25, hosts[0])

    results = await verify_candidates(
        make_contact(),
        [make_candidate("not-an-email"), make_candidate("later@example.test")],
        make_settings(),
        mx_resolver=mx,
        probe_fn=probe,
    )

    assert results[0].verification_verdict is VerificationVerdict.KILL
    assert results[0].notes.endswith("invalid syntax")
    assert results[1].verification_verdict is VerificationVerdict.HOLD


@pytest.mark.asyncio
async def test_mail_from_rejection_is_unknown_for_candidate() -> None:
    async def mx(domain: str) -> list[str]:
        return ["mx.example.test"]

    async def sender_rejected(*args: object) -> Probe:
        return Probe(550, "sender refused by policy", 25, "mx.example.test", sender_refused=True)

    [result] = await verify_candidates(
        make_contact(),
        [make_candidate("jane@example.test")],
        make_settings(),
        mx_resolver=mx,
        probe_fn=sender_rejected,
    )

    assert result.verification_verdict is VerificationVerdict.UNKNOWN
    assert "MAIL FROM rejected" in result.notes


@pytest.mark.asyncio
async def test_results_keep_original_input_order_across_domains() -> None:
    async def mx(domain: str) -> list[str]:
        await asyncio.sleep(0.01 if domain == "example.test" else 0)
        return [f"mx.{domain}"]

    async def probe(hosts: list[str], address: str, *args: object) -> Probe:
        return Probe(550, "5.1.1 user unknown", 25, hosts[0])

    candidates = [
        make_candidate("first@example.test"),
        make_candidate("only@other.test"),
        make_candidate("third@example.test"),
    ]
    results = await verify_candidates(
        make_contact(), candidates, make_settings(), mx_resolver=mx, probe_fn=probe
    )

    assert [result.email for result in results] == [candidate.email for candidate in candidates]


@pytest.mark.asyncio
async def test_concurrency_is_limited_per_domain() -> None:
    active = 0
    maximum_active = 0

    async def mx(domain: str) -> list[str]:
        return ["mx.example.test"]

    async def probe(hosts: list[str], address: str, *args: object) -> Probe:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return Probe(550, "5.1.1 user unknown", 25, hosts[0])

    candidates = [make_candidate(f"person{index}@example.test") for index in range(20)]
    await verify_candidates(
        make_contact(), candidates, make_settings(), mx_resolver=mx, probe_fn=probe
    )

    assert maximum_active == 3


@pytest.mark.asyncio
async def test_missing_mail_from_is_a_clear_configuration_error() -> None:
    with pytest.raises(RuntimeError, match="LEADFORGE_SMTP_MAIL_FROM"):
        await verify_candidates(
            make_contact(),
            [make_candidate("jane@example.test")],
            make_settings(smtp_mail_from=None),
        )


@pytest.mark.asyncio
async def test_port_25_check_reports_reachable_with_fake_resolver_and_socket() -> None:
    calls: list[tuple[str, int]] = []

    class Writer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    async def mx(domain: str) -> list[str]:
        assert domain == "gmail.com"
        return ["mx.example.test"]

    async def open_connection(host: str, port: int):
        calls.append((host, port))
        return object(), Writer()

    assert await check_port25_status(mx_resolver=mx, socket_connector=open_connection) is (
        Port25Status.REACHABLE
    )
    assert calls == [("mx.example.test", 25)]


@pytest.mark.asyncio
async def test_port_25_check_distinguishes_dns_failure_from_tcp_timeout() -> None:
    async def dns_failure(domain: str) -> list[str]:
        assert domain == "gmail.com"
        raise dns.exception.Timeout("DNS timed out")

    async def mx(domain: str) -> list[str]:
        return ["mx.example.test"]

    async def timeout_socket(host: str, port: int):
        assert (host, port) == ("mx.example.test", 25)
        raise TimeoutError("TCP connect timed out")

    assert await check_port25_status(mx_resolver=dns_failure) is Port25Status.DNS_FAILED
    assert (
        await check_port25_status(mx_resolver=mx, socket_connector=timeout_socket)
        is Port25Status.BLOCKED
    )


@pytest.mark.asyncio
async def test_port_25_check_reports_dns_failure_when_no_mx_records_exist() -> None:
    async def no_mx(domain: str) -> list[str]:
        return []

    async def unexpected_socket(host: str, port: int):
        raise AssertionError("no socket call without MX records")

    assert (
        await check_port25_status(mx_resolver=no_mx, socket_connector=unexpected_socket)
        is Port25Status.DNS_FAILED
    )


class SmtpHandler:
    def __init__(self) -> None:
        self.catch_all = False
        self.data_commands = 0

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        envelope.rcpt_tos.append(address)
        if self.catch_all or address == "good@local.test":
            return "250 2.1.5 recipient accepted"
        if address == "later@local.test":
            return "451 4.7.1 try again later"
        return "550 5.1.1 user unknown"

    async def handle_DATA(self, server, session, envelope):
        self.data_commands += 1
        raise AssertionError("SMTP DATA must never be sent")


@pytest.mark.asyncio
async def test_local_smtp_rcpt_verification_never_sends_data() -> None:
    with socket.socket() as probe_socket:
        probe_socket.bind(("127.0.0.1", 0))
        port = probe_socket.getsockname()[1]

    handler = SmtpHandler()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()

    async def local_mx(domain: str) -> list[str]:
        return ["127.0.0.1"]

    candidates = [
        make_candidate("good@local.test"),
        make_candidate("bad@local.test"),
        make_candidate("later@local.test"),
    ]
    try:
        results = await verify_candidates(
            make_contact("local.test"),
            candidates,
            make_settings(),
            ports=(port,),
            mx_resolver=local_mx,
        )
        assert [candidate.verification_verdict for candidate in results] == [
            VerificationVerdict.SEND,
            VerificationVerdict.KILL,
            VerificationVerdict.HOLD,
        ]

        handler.catch_all = True
        [catch_all_result] = await verify_candidates(
            make_contact("local.test"),
            [make_candidate("good@local.test")],
            make_settings(),
            ports=(port,),
            mx_resolver=local_mx,
        )
        assert catch_all_result.catch_all_domain is True
        assert catch_all_result.confidence == 0.4
        assert handler.data_commands == 0
    finally:
        controller.stop()
