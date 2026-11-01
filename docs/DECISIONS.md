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
- Tier 3 is now implemented (`web/tiers/browser.py`) rather than stubbed. It prefers Patchright over stock Playwright because Patchright removes the `Runtime.enable`/CDP artefacts that anti-bot vendors fingerprint; the fallback keeps the tier usable on machines without it. A persistent `data/browser-profile` user-data directory is reused across navigations so a manual sign-in survives between commands, and page recycling happens after `browser_max_pages_per_session` navigations.
- Tier 3 is gated three ways and defaults to *off*: `LEADFORGE_BROWSER_ENABLED=false`, `allow_browser = false` on any rule that has not opted in, and `max_tier < 3`. The `[default]` rule now reaches Tier 3 so company career/contact pages can render, but nothing launches until the process switch is on — with it off, a thin page fails as `FetchFailed` exactly as before.
- Challenge handling stays human: when a headed navigation lands on a challenge page, the tier logs it, waits up to `browser_manual_challenge_wait_s` for the operator to clear it in the visible window, and fails the fetch if it is not cleared. Nothing solves challenges programmatically; `allow_challenge_solving` remains false on every rule.
- The router escalates a `blocked`/`challenge` classification to Tier 3 instead of pausing the host, but only when the domain allows the browser and the process switch is on. Every escalation still acquires a budget slot, so the per-domain spacing and daily cap remain the real limiter.
- Tier 3 supersedes the earlier "intentionally stays off" entry (2026-10-31 operator decision); the benchmark is no longer a precondition because the probe is now a real browser rather than a theoretical tier.
- Engine selection is a ladder, not a hard preference. Patchright pins its own Chromium build; a machine that has the package but never ran `patchright install chromium` fails at launch with "Executable doesn't exist". Startup therefore tries patchright, then playwright, and for each engine the configured `channel` (default `chrome`, i.e. the installed browser) then the bundled build, and keeps the first combination that launches. `leadforge doctor` prints which engines are importable. Live verification on this machine: bundled Chromium and installed Chrome both returned `example.com` in ~2-3 s headless and headed; a router-level `min_tier=3` fetch returned tier 3 and the second call came from cache.
- The live Tier 1 acceptance fetch and persistent-cache repeat succeeded against `example.com` after the sandbox network restriction was lifted for those checks. The Windows system resolver timed out for the initial dnspython MX lookup; explicit 1.1.1.1/8.8.8.8 UDP and TCP probes succeeded, and the routed doctor check with 1.1.1.1 reported port 25 reachable. `dns_nameservers` remains empty by default so system resolution is still the default.
- Scrapling's HTTP response body was empty for public JSON APIs in the live endpoint checks. Those approved ATS and Hacker News API hosts therefore use HTTPX as the Tier 1 transport inside the same fetch router; budgets, robots handling, block pauses, and fetch logs still apply.
- Proxies are configured once (`LEADFORGE_PROXY_URL`, `scheme://user:pass@host:port`, http/https/socks5) and adapted per transport in `web/proxy.py`: one inline URL for `httpx` and `curl_cffi`, and `{server, username, password}` for Playwright. `LEADFORGE_PROXY_TIER1/2/3` allow disabling one tier without touching the others. There is deliberately **no silent fallback**: if a proxy is configured, a dead proxy fails the fetch (`ConnectError` at Tier 1, `net::ERR_PROXY_CONNECTION_FAILED` at Tier 3, `--proxy` at Tier 2) rather than quietly scraping from the host IP — verified both ways on this machine. Credentials are never logged; `doctor` prints only `host:port` via `redact()`.

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
- People search has offline replay, router-backed SearxNG, and a logged-out DuckDuckGo HTML adapter. The DDG parser is based on the single approved `Stripe engineering blog` response snapshot; result URLs are never fetched.
- The manual saved-page importer is fully offline, deterministic, and limited to professional fields. Ambiguous or changed markup is reported as unrecognised rather than guessed.

## Enrichment outputs

- Live discovery, people search, walk, and enrichment require the operator's companies CSV and an ingested resume profile. Replay modes remain offline and do not require a real SMTP identity or LLM key.
- The sample enrichment fixture demonstrates a published Tier-A address at `.invalid`; it stays below the 0.40 send threshold, so the enriched output shows it as `unknown` and the review file remains empty. Drafts are manual-personalization markers unless professional project evidence exists.
- The tier benchmark is implemented with a no-request `--dry-run` mode. No live benchmark was run because the operator domain list is absent.

## Company seeding and run metrics

- `seed-companies` uses the operator's source order. Existing HN API, ATS JSON, and approved DDG response parsers are reused. YC, Work at a Startup, and the four public job-platform HTML sources remain unrecognised-only until their real markup is available; unsupported markup is never guessed.
- Every live page request in seeding uses the shared router and host policy. DNS and homepage validation are injectable for offline tests. Seeding accepts a registrable official domain only after DNS resolution and a homepage-name check, plus a matching early-career role or an explicit invitation email signal. It requires at least 8 accepted companies and ranks invite signals before match score while enforcing an early-stage mix when enough candidates exist.
- `--from-cache` reads the page table with SQLite `mode=ro`; its company/job persistence targets `data/cache/seed-replay.db`, not the operator database. The specified `data/cache/leadforge.db` contained zero page rows, so no real YC, Work at a Startup, or job-platform markup was available. Those sources remain unrecognised-only until their actual cached pages can be inspected.
- `export-run-summary` aggregates fixed source labels and database counts through a read-only connection. Average enrichment fetches come only from the numeric `fetches_used` column in the latest enrichment CSV; row text is never copied to the summary. Tests use fictional names, addresses, and query URLs and verify those values do not appear in the JSON.
- No live discovery, search, SMTP check, LLM call, or mutation of the operator cache database was performed in this offline build.
