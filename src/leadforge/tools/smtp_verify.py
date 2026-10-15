"""Non-sending SMTP recipient verification using MAIL FROM and RCPT TO only."""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import string
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

import aiosmtplib
import dns.asyncresolver
import dns.exception
import dns.resolver

from leadforge.config import Settings
from leadforge.models import (
    Contact,
    EmailCandidate,
    VerificationMethod,
    VerificationVerdict,
)

log = logging.getLogger(__name__)

PORTS = (25, 587, 465)
MAX_MX_HOSTS = 3
UNCERTAIN_CAP = 0.40

# Pragmatic subset of RFC 5322: reject junk without trying to support every legal edge case.
_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
_POLICY_HINTS = (
    "spamhaus",
    "blocked",
    "blacklist",
    "blocklist",
    "reputation",
    "policy",
    "denied",
    "not allowed",
    "banned",
    "rbl",
    "ptr",
)


@dataclass(frozen=True)
class Probe:
    code: int
    message: str
    port: int
    host: str
    sender_refused: bool = False


class Unreachable(Exception):
    """No MX host accepted a connection on any configured port."""


MxResolver = Callable[[str], Awaitable[list[str]]]
ProbeFn = Callable[[list[str], str, str, str, float, tuple[int, ...]], Awaitable[Probe]]


class Port25Status(str, Enum):  # noqa: UP042 - preserve string-enum behavior
    DNS_FAILED = "dns resolution failed"
    BLOCKED = "port 25 blocked (tcp timeout)"
    REACHABLE = "reachable"


def is_valid_syntax(address: str) -> bool:
    return bool(_EMAIL_RE.fullmatch(address))


def smtp_mail_from_problem(address: str | None) -> str | None:
    """Return a setup error when MAIL FROM is empty, malformed, or a placeholder."""
    if not address or not address.strip():
        return "Set LEADFORGE_SMTP_MAIL_FROM to a real address on a domain you control."
    if not is_valid_syntax(address):
        return "LEADFORGE_SMTP_MAIL_FROM must be a valid email address."
    domain = address.rsplit("@", 1)[-1].lower()
    if domain in {"yourdomain.com", "example.com", "example.org", "example.net"} or domain.endswith(
        (".example", ".test")
    ):
        return "Replace the placeholder LEADFORGE_SMTP_MAIL_FROM with a real address."
    return None


def classify(code: int, message: str = "") -> str:
    """Map a recipient reply to send, hold, kill, or unknown."""
    if code in (250, 251):
        return "send"
    if code in (450, 451, 452):
        return "hold"
    if code in (550, 551, 553):
        if any(hint in message.lower() for hint in _POLICY_HINTS):
            return "unknown"
        return "kill"
    return "unknown"


async def resolve_mx(
    domain: str,
    timeout_seconds: float = 8.0,
    *,
    raise_on_error: bool = False,
    nameservers: list[str] | None = None,
) -> list[str]:
    """Return MX hosts; empty means a genuine negative or empty DNS answer.

    Resolver failures are deliberately raised so callers cannot confuse a broken
    lookup with a domain that cannot receive mail.
    """
    del raise_on_error  # Kept for source compatibility; resolver errors always propagate.
    resolver = dns.asyncresolver.Resolver(configure=True)
    if nameservers is None:
        nameservers = Settings().dns_nameservers
    if nameservers:
        resolver.nameservers = nameservers
    try:
        answer = await resolver.resolve(domain, "MX", lifetime=timeout_seconds)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.DNSException, OSError, TimeoutError):
        raise
    records = sorted(answer, key=lambda record: record.preference)
    # RFC 7505 null MX explicitly means this domain does not accept mail.
    if any(str(record.exchange) == "." for record in records):
        return []
    return [host for record in records if (host := str(record.exchange).rstrip("."))]


async def _probe_host(
    host: str,
    address: str,
    mail_from: str,
    helo: str,
    timeout_seconds: float,
    ports: tuple[int, ...],
) -> Probe:
    last_exc: Exception | None = None
    for port in ports:
        client = aiosmtplib.SMTP(
            hostname=host,
            port=port,
            use_tls=(port == 465),
            start_tls=False if port == 465 else None,
            validate_certs=True,
            timeout=timeout_seconds,
            local_hostname=helo,
        )
        try:
            await client.connect()
            await client.ehlo()
            try:
                await client.mail(mail_from)
            except aiosmtplib.SMTPSenderRefused as exc:
                return Probe(exc.code, str(exc.message), port, host, sender_refused=True)
            try:
                response = await client.rcpt(address)
                return Probe(response.code, str(response.message), port, host)
            except aiosmtplib.SMTPRecipientRefused as exc:
                return Probe(exc.code, str(exc.message), port, host)
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            last_exc = exc
        finally:
            try:
                await client.quit()
            except Exception:
                client.close()
    raise Unreachable(f"{host}: {last_exc!r}")


async def _probe(
    hosts: list[str],
    address: str,
    mail_from: str,
    helo: str,
    timeout_seconds: float,
    ports: tuple[int, ...],
) -> Probe:
    last_exc: Exception | None = None
    for host in hosts[:MAX_MX_HOSTS]:
        try:
            return await _probe_host(host, address, mail_from, helo, timeout_seconds, ports)
        except Unreachable as exc:
            last_exc = exc
    raise Unreachable(str(last_exc or "no MX hosts configured"))


async def check_port25_status(
    timeout_seconds: float = 5.0,
    *,
    mx_resolver: MxResolver | None = None,
    socket_connector: Callable[..., Awaitable[tuple[object, object]]] | None = None,
) -> Port25Status:
    """Distinguish DNS failure from TCP timeout while checking a Gmail MX."""
    try:
        if mx_resolver is None:
            hosts = await resolve_mx(
                "gmail.com", timeout_seconds=timeout_seconds, raise_on_error=True
            )
        else:
            hosts = await mx_resolver("gmail.com")
    except (dns.exception.DNSException, OSError, TimeoutError) as exc:
        log.info("Port 25 diagnostic DNS failure: %s", exc)
        return Port25Status.DNS_FAILED
    if not hosts:
        return Port25Status.DNS_FAILED
    connector = socket_connector or asyncio.open_connection
    for host in hosts[:MAX_MX_HOSTS]:
        try:
            _, writer = await asyncio.wait_for(connector(host, 25), timeout=timeout_seconds)
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=timeout_seconds)
            except (OSError, TimeoutError):
                pass
            return Port25Status.REACHABLE
        except (OSError, TimeoutError):
            continue
    return Port25Status.BLOCKED


def _random_local(n: int = 12) -> str:
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(n))


async def verify_candidates(
    contact: Contact,
    candidates: list[EmailCandidate],
    settings: Settings | None = None,
    *,
    ports: tuple[int, ...] = PORTS,
    mx_resolver: MxResolver | None = None,
    probe_fn: ProbeFn | None = None,
) -> list[EmailCandidate]:
    """Verify candidates and return updated copies in the original input order."""
    if settings is None:
        settings = Settings()
    mail_from = settings.smtp_mail_from
    mail_from_problem = smtp_mail_from_problem(mail_from)
    if mail_from_problem:
        raise RuntimeError(mail_from_problem)

    delay = max(0.0, settings.request_delay_seconds)
    max_concurrency = max(1, settings.smtp_max_concurrent)
    timeout_seconds = settings.smtp_timeout_seconds
    helo = mail_from.split("@", 1)[-1]
    selected_probe = probe_fn or _probe

    by_domain: dict[str, list[tuple[int, EmailCandidate]]] = {}
    for index, candidate in enumerate(candidates):
        domain = candidate.email.rsplit("@", 1)[-1].lower()
        by_domain.setdefault(domain, []).append((index, candidate))

    indexed_results: dict[int, EmailCandidate] = {}

    async def run_domain(domain: str, group: list[tuple[int, EmailCandidate]]) -> None:
        valid = [
            (index, candidate) for index, candidate in group if is_valid_syntax(candidate.email)
        ]
        for index, candidate in group:
            if not is_valid_syntax(candidate.email):
                indexed_results[index] = _apply(
                    candidate,
                    VerificationVerdict.KILL,
                    VerificationMethod.SMTP,
                    False,
                    "invalid syntax",
                )
        if not valid:
            return

        try:
            if mx_resolver is None:
                hosts = await resolve_mx(
                    domain, timeout_seconds, nameservers=settings.dns_nameservers
                )
            else:
                hosts = await mx_resolver(domain)
        except Exception as exc:
            log.warning("MX lookup failed for %s: %s", domain, exc)
            for index, candidate in valid:
                indexed_results[index] = _apply(
                    candidate,
                    VerificationVerdict.UNKNOWN,
                    VerificationMethod.PATTERN_ONLY,
                    False,
                    f"dns error: {exc}",
                )
            return
        if not hosts:
            for index, candidate in valid:
                indexed_results[index] = _apply(
                    candidate,
                    VerificationVerdict.KILL,
                    VerificationMethod.SMTP,
                    False,
                    "no MX records",
                )
            return

        semaphore = asyncio.Semaphore(max_concurrency)
        pacing_lock = asyncio.Lock()
        last_started: float | None = None

        async def throttled(address: str) -> Probe:
            nonlocal last_started
            async with semaphore:
                async with pacing_lock:
                    loop = asyncio.get_running_loop()
                    now = loop.time()
                    if last_started is not None:
                        wait = last_started + delay - now
                        if wait > 0:
                            await asyncio.sleep(wait)
                    last_started = loop.time()
                return await selected_probe(hosts, address, mail_from, helo, timeout_seconds, ports)

        catch_all = False
        try:
            catch_all_probe = await throttled(f"{_random_local()}@{domain}")
            catch_all = not catch_all_probe.sender_refused and catch_all_probe.code in (250, 251)
        except Unreachable as exc:
            note = f"SMTP unreachable on {ports}: {exc}"
            for index, candidate in valid:
                indexed_results[index] = _apply(
                    candidate,
                    VerificationVerdict.UNKNOWN,
                    VerificationMethod.PATTERN_ONLY,
                    False,
                    note,
                )
            return

        async def verify_one(index: int, candidate: EmailCandidate) -> None:
            try:
                probe = await throttled(candidate.email)
            except Unreachable as exc:
                indexed_results[index] = _apply(
                    candidate,
                    VerificationVerdict.UNKNOWN,
                    VerificationMethod.PATTERN_ONLY,
                    catch_all,
                    f"unreachable: {exc}",
                )
                return
            verdict = (
                VerificationVerdict.UNKNOWN
                if probe.sender_refused
                else VerificationVerdict(classify(probe.code, probe.message))
            )
            note = f"{probe.host}:{probe.port} -> {probe.code} {probe.message[:120]}"
            if probe.sender_refused:
                note = f"MAIL FROM rejected; candidate not checked: {note}"
            if catch_all:
                note += " | catch-all domain, verdict unreliable"
            indexed_results[index] = _apply(
                candidate, verdict, VerificationMethod.SMTP, catch_all, note
            )

        await asyncio.gather(*(verify_one(index, candidate) for index, candidate in valid))

    await asyncio.gather(*(run_domain(domain, group) for domain, group in by_domain.items()))
    return [indexed_results[index] for index in range(len(candidates))]


def _apply(
    candidate: EmailCandidate,
    verdict: VerificationVerdict,
    method: VerificationMethod,
    catch_all: bool,
    note: str,
) -> EmailCandidate:
    confidence = candidate.confidence
    if verdict is VerificationVerdict.KILL:
        confidence = 0.0
    elif catch_all or method is VerificationMethod.PATTERN_ONLY:
        confidence = min(confidence, UNCERTAIN_CAP)
    prior_notes = f"{candidate.notes} | " if candidate.notes else ""
    values = candidate.model_dump()
    values.update(
        verification_method=method,
        verification_verdict=verdict,
        catch_all_domain=catch_all,
        confidence=confidence,
        notes=prior_notes + note,
    )
    return EmailCandidate.model_validate(values)


def verify_candidates_sync(
    contact: Contact,
    candidates: list[EmailCandidate],
    settings: Settings | None = None,
    **kwargs: object,
) -> list[EmailCandidate]:
    return asyncio.run(verify_candidates(contact, candidates, settings, **kwargs))
