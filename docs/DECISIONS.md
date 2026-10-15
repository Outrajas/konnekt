# Decisions

## Environment and dependencies

- The repository plan names Python 3.12.10, but that interpreter was unavailable. The existing `.venv` pointed to a missing installation, so it was rebuilt with the available bundled Python 3.12.14. The project metadata accepts Python 3.12 patch releases (`>=3.12,<3.13`).
- Scrapling remains an optional `fast` extra; no browser installation is part of the baseline.
- Empty environment placeholders are ignored by settings, so copying `.env.example` leaves safe code defaults active.
- The original string enums remain `str, Enum` rather than `StrEnum`; Ruff's modernization warning is suppressed because changing `str(enum_value)` would alter existing runtime behavior.

## Platform policy

- The operator explicitly enabled logged-out LinkedIn, Indeed, and Glassdoor access with a 30–90 second spacing range, a 10-request daily cap per platform, and Tier 1 as the maximum. LinkedIn page fetches are limited to snippet misses and never bulk crawled; Indeed and Glassdoor are limited to job and hiring signals. No login, operator cookies, credentials, challenge solving, or stealth tier is allowed for these hosts. A block pauses the host for 48 hours.

## Web fetch baseline

- Tier 1 uses Scrapling's HTTP `Fetcher` when importable, isolated in a worker thread; otherwise it uses `httpx`. Neither path persists cookies or accepts credentials. `tldextract` uses its bundled suffix data to avoid an unplanned network update during policy resolution.
- Tier 2 is implemented as a subprocess adapter but remains unavailable until Obscura is installed and its CLI flags are verified in Phase 7. Tier 3 remains unavailable in this baseline.
- Tier 3 intentionally stays off; revisit only if the Phase 16 benchmark shows a concrete need.
- The live Tier 1 acceptance fetch and persistent-cache repeat succeeded against `example.com` after the sandbox network restriction was lifted for those checks. Doctor's independent port 25 probe remains blocked by DNS/network egress in this environment.

## Pattern evidence and scoring

- `EmailCandidate.evidence_tier` records the Phase 11 A/B/C evidence class as an additive field alongside the original spec model; see `SPEC_DELTA.md` §5 entry. Confidence weights remain exactly those in spec §6.
- Domain pattern learning stores unique Tier-A email samples by domain and address, then keeps the majority pattern in `domain_pattern`; ties preserve the prior winner or use stable pattern ordering. A pattern is established only with two unique samples agreeing on that pattern.

## Extraction helpers

- `classify_host` accepts an optional `company_domain`, because a URL alone cannot identify whether an otherwise generic host belongs to the company being evaluated. Conference hosts and configured personal-site domains live in `data/host_kinds.toml`.
- Email extraction drops reserved example/test domains, including `.example`, even when an address is syntactically valid; fixtures therefore test their exclusion instead of treating them as deliverable addresses.
