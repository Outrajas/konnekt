"""Who the outreach is from, and whether the sending domain is actually ready.

A domain you own is only half the email identity: unless it publishes MX (so
replies arrive) and SPF/DMARC (so receivers can tell your mail from a forgery),
`verify@yourdomain` is just a string. This module answers both questions: who
the operator is, and whether the domain can carry that identity yet.
"""

from __future__ import annotations

import dns.exception
import dns.resolver

from leadforge.config import Settings


def signature(settings: Settings) -> str:
    """Plain-text sign-off appended to every draft, or '' when unset."""
    name = (settings.operator_name or "").strip()
    email = (settings.operator_email or "").strip()
    if not name and not email:
        return ""
    lines = [line for line in (name, email) if line]
    return "\n\n--\n" + "\n".join(lines)


def sending_address(settings: Settings) -> str:
    """The address outreach will be sent from (drafts show it; nothing sends)."""
    return (settings.operator_email or settings.smtp_mail_from or "").strip()


def _txt(name: str) -> list[str]:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5
    try:
        answer = resolver.resolve(name, "TXT")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers,
            dns.exception.DNSException):
        return []
    return ["".join(part.decode() if isinstance(part, bytes) else str(part)
                     for part in record.strings)
            for record in answer]


def _records(name: str, kind: str) -> list[str]:
    resolver = dns.resolver.Resolver()
    resolver.lifetime = 5
    try:
        return [str(record).strip() for record in resolver.resolve(name, kind)]
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers,
            dns.exception.DNSException):
        return []


def check_domain(domain: str) -> dict[str, object]:
    """Everything a receiving mail server will ask about this domain."""
    domain = domain.strip().lower()
    spf = [record for record in _txt(domain) if record.lower().startswith("v=spf1")]
    dmarc_records = _txt(f"_dmarc.{domain}")
    dmarc = [r for r in dmarc_records if r.lower().startswith("v=dmarc1")]
    mx = _records(domain, "MX")
    dkim = bool(_txt(f"default._domainkey.{domain}"))
    a = _records(domain, "A")

    return {
        "domain": domain,
        "a": a,
        "mx": mx,
        "spf": spf,
        "dmarc": dmarc,
        "dkim_default": dkim,
        "can_send": bool(spf),
        "can_receive": bool(mx),
        "guidance": _guidance(domain, bool(mx), bool(spf), bool(dmarc)),
    }


def _guidance(domain: str, has_mx: bool, has_spf: bool, has_dmarc: bool) -> list[str]:
    tips: list[str] = []
    if not has_mx:
        tips.append(
            f"Add MX for {domain}: free options are Cloudflare Email Routing "
            "(enable in the dashboard) or ImprovMX — without it, replies bounce."
        )
    if not has_spf:
        tips.append(
            f"Add TXT on {domain}: 'v=spf1 include:<your-mail-provider> ~all' "
            "(or 'v=spf1 -all' if the domain never sends)."
        )
    if not has_dmarc:
        tips.append(
            f"Add TXT on _dmarc.{domain}: 'v=DMARC1; p=none; rua=mailto:you@{domain}' "
            "— start at p=none and only tighten after a month of clean reports."
        )
    if has_spf and not has_dmarc:
        tips.append("SPF without DMARC is scored as unaligned by Gmail and Outlook.")
    return tips
