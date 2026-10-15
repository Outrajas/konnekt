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
- Tier 2 is implemented as a subprocess adapter and remains unavailable until its path is explicitly configured. Tier 3 remains unavailable in this baseline.
- The operator-provided Obscura `0.2.4` and worker are present together under ignored `bin/obscura/`. The help output shows `--quiet` is a `fetch` option, so the adapter places it after the subcommand. Both text and HTML fetch smoke checks against `https://example.com` passed. The release ZIP/tag was not present to verify; the local executable SHA-256 values are `42CB7BEF9EF4C946312E59411D69B12164146D95A5C8E40B92E275347E124CC1` (`obscura.exe`) and `2609011D104BCE4724A1968D802CD71BA55F283EB285DBB1570099B1C4DD25E3` (`obscura-worker.exe`). Tier 2 stays disabled until `OBSCURA_PATH` is configured.
- Tier 3 intentionally stays off; revisit only if the Phase 16 benchmark shows a concrete need.
- The live Tier 1 acceptance fetch and persistent-cache repeat succeeded against `example.com` after the sandbox network restriction was lifted for those checks. The Windows system resolver timed out for the initial dnspython MX lookup; explicit 1.1.1.1/8.8.8.8 UDP and TCP probes succeeded, and the routed doctor check with 1.1.1.1 reported port 25 reachable. `dns_nameservers` remains empty by default so system resolution is still the default.
- Scrapling's HTTP response body was empty for public JSON APIs in the live endpoint checks. Those approved ATS and Hacker News API hosts therefore use HTTPX as the Tier 1 transport inside the same fetch router; budgets, robots handling, block pauses, cache, and fetch logs still apply.

## Pattern evidence and scoring

- `EmailCandidate.evidence_tier` records the Phase 11 A/B/C evidence class as an additive field alongside the original spec model; see `SPEC_DELTA.md` §5 entry. Confidence weights remain exactly those in spec §6.
- Domain pattern learning stores unique Tier-A email samples by domain and address, then keeps the majority pattern in `domain_pattern`; ties preserve the prior winner or use stable pattern ordering. A pattern is established only with two unique samples agreeing on that pattern.

## Bounded person walk

- The `--replay` walk mode uses only an explicit manifest and fixture files, and bypasses LLM and SMTP calls even when live settings exist. This keeps the demo fully offline and makes recorded results reproducible.
- A public email is attributed to the target only when the validated page extraction associates it with an exact target-name mention, or the deterministic fallback finds the name and a single address in the same text paragraph.

## Extraction helpers

- `classify_host` accepts an optional `company_domain`, because a URL alone cannot identify whether an otherwise generic host belongs to the company being evaluated. Conference hosts and configured personal-site domains live in `data/host_kinds.toml`.
- Email extraction drops reserved example/test domains, including `.example`, even when an address is syntactically valid; fixtures therefore test their exclusion instead of treating them as deliverable addresses.

## Discovery inputs

- ATS response shapes were checked against the public Stripe Greenhouse board, the Lever vendor example board, and Linear's Ashby board. The first guessed Greenhouse and Lever identifiers returned 404, so the recorded snapshots use valid boards. Snapshots retain only a small set of returned records; Ashby email strings were redacted before commit.
- No operator company list or resume was present in `data/input`; no live company discovery, people search, or resume ingestion was run. Live discovery is gated on both the supplied companies CSV and an editable `data/profile.json` created from the resume.
