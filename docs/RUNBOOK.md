# Leadforge runbook

Run operator commands from the repository root in PowerShell after activating `.venv`. The operator uses `.\.venv`; the agent uses `.\.venv-agent`.

```powershell
.\.venv\Scripts\Activate.ps1
leadforge version
leadforge doctor
leadforge init-db
```

`doctor` reports local configuration, daily platform budgets, environment variable names (never values), and the port-25 probe result with a remedy. `init-db` creates or migrates the local SQLite database.

## Database and outreach tracking

`leadforge backup` writes a consistent database copy to the ignored `data/backups/` folder. To recreate an empty schema, run `leadforge reset-db --confirm` and type `RESET LEADFORGE DATABASE` when prompted.

The outreach tracker only records work the operator has done manually; it never sends messages. Preview the currently eligible list with `leadforge today`. After manually sending a Tier A message, record it with `leadforge mark-sent <person_id> <email>`. Record a later response with `leadforge mark-outcome <person_id> <email> <outcome>` where outcome is `reply`, `bounce`, `auto_reply`, or `opt_out`. Suppress an exact address or whole domain with `leadforge suppress <email-or-domain>`. The cap ramps from 5 per day in the first week to 10 in the second week and 20 thereafter. Suppressed and contacted people are removed from the review file and today's list.

Run the capped daily sequence with `leadforge run-daily --dry-run` to preview it and `leadforge run-daily` to start it. The live run keeps a lock while active and records completed stages so it can resume after interruption. It stops when a routed fetch is blocked. Check existing draft quality with `leadforge drafts --check`; drafts with missing professional evidence remain marked `NEEDS_MANUAL_PERSONALIZATION` in `review.csv`.

## Fetch a page

```powershell
leadforge fetch https://company.example/contact --text
```

The fetch router applies host policy, robots rules, budgets, block pauses, and cache behavior. Tier 2 remains unavailable until `OBSCURA_PATH` points to the verified binary.

## Verify a CSV

The CSV needs `first_name`, `last_name`, `domain`, and `email` columns.

```powershell
leadforge verify data/input/candidates.csv
```

SMTP verification performs the non-sending envelope check described in the specification. Never use candidate addresses that you do not have a legitimate reason to assess.

## Walk one person

```powershell
leadforge walk --company-domain acme.example --first Jane --last Doe --title "Engineering Manager"
```

This uses the configured fetch and LLM layers, deterministic link ordering when the LLM is not configured, the bounded walk limits, the SMTP verifier, and local persistence.

For a fully offline demo, use the recorded fixture set:

```powershell
leadforge walk --company-domain acme.invalid --first Jane --last Doe --title "Engineering Manager" --replay data/fixtures/replay/person_walk
```

Replay mode reads only the fixture manifest and HTML files. It makes no HTTP, LLM, or SMTP requests.

## LLM connectivity

`leadforge llm-ping` sends one small JSON request to the configured OpenAI-compatible endpoint. It prints `LLM not configured` and exits successfully when the three LLM settings are missing. The automated tests use fake HTTP responses; do not run the ping until the operator authorizes a live smoke test.
# SMTP verification setup

`LEADFORGE_SMTP_VERIFY_MODE` accepts `off` (default), `null_sender`, or `domain`.
`off` makes no SMTP recipient probes and only Tier A candidates can be sendable.
`null_sender` probes with the SMTP null sender (`MAIL FROM:<>`). `domain` uses
`SMTP_MAIL_FROM` or `LEADFORGE_SMTP_MAIL_FROM`, which must be a real address at
a domain the operator controls. A refused MAIL FROM is `unknown`; no mode sends
message data.

## Discovery inputs

Place the operator resume in `data/input`, run `leadforge profile ingest <resume>`
and review `data/profile.json`. Add only operator-approved companies to
`data/input/companies.csv` (`name,domain`). Preview the routed requests with
`leadforge discover --companies data/input/companies.csv --dry-run`; a live run
is refused until the editable profile exists. Use `--include-hn` to add the
Hacker News hiring thread source.

People search works offline with `leadforge people --company Acme --domain
acme.example --replay data/fixtures/people/sample`. Live search requires the
company list, an ingested profile, and an explicitly configured provider; only
result snippets are parsed, and result pages are never fetched.

## Manual research workflow

Browse manually while logged out, then use the browser's **Ctrl+S** to save
LinkedIn profile or people-search results, Indeed/Glassdoor job pages, Glassdoor
company pages, or company pages into `data/input/saved_pages/`. Run
`leadforge import-pages --dry-run` to preview recognized work fields, then
`leadforge import-pages` to save those fields as local people, jobs, and
professional evidence. Add `--delete-after` to remove recognized source files
after import. The importer does not fetch pages or make network calls. Continue
with `leadforge people` and `leadforge enrich` after the operator's company list
and resume profile are present. Changed or ambiguous markup is reported as
unrecognised. The library has an optional injected local-model fallback for
ambiguous fields with Phase 10 cross-checks; the CLI does not contact a remote
LLM so the offline guarantee remains true.

## Replay, review, and maintenance

Run the local demo with `leadforge enrich tests/fixtures/sample_contacts.csv
--replay data/fixtures/sample --force`, then inspect the enriched CSV and
`data/output/review.csv`. The review file includes only sendable candidates and
leaves `approved` empty. `leadforge drafts --dry-run` previews draft count;
`leadforge drafts` writes `NEEDS_MANUAL_PERSONALIZATION` when no professional
project evidence is available. No command sends mail.

Use `leadforge report --since 30` for recent counts and source yields. Expired
records and cache files can be removed with `leadforge purge --older-than 30`;
`leadforge purge --all` requires typing `ERASE ALL LEADFORGE RECORDS`. After
reviewing a block reason, clear a host pause with `leadforge domains unpause
<host>`.

## Company seeding and daily pipeline

Use `leadforge seed-companies --dry-run` to inspect the ordered source plan and
policy caps without making requests. Then run `leadforge seed-companies` to
build `data/input/companies.csv` and its ignored metadata file from approved
hiring sources. At least 8 companies must pass the official-domain, DNS,
homepage-name, hiring-signal, role/location, deduplication, and agency checks.
If fewer than 8 pass, seeding stops without replacing the companies file and
prints per-source counts. When 8–14 pass, the documented fallback allows
continuing with that set.

`leadforge run-daily --seed` seeds first only when `companies.csv` is missing
or has no company rows. The normal daily stages are discover, people, enrich,
then report. `--dry-run` previews the work. Use
`leadforge export-run-summary` to write the counts-only JSON summary. Follow
[LIVE_RUN.md](LIVE_RUN.md) for the full command order and stop/block meanings.

## Tier benchmark

Create `data/input/benchmark_domains.txt` with operator-approved domains, one
per line. Preview policy and budget scope with `python
scripts/benchmark_tiers.py --domains data/input/benchmark_domains.txt --dry-run`.
The live benchmark uses the router and may request Tier 2 only when the
configured Obscura binary exists; review the domain list before running it.

## Live browser run (Tier 3)

Tier 3 launches a real, headed Chrome using the persistent profile in
`data/browser-profile`. Sign in to a platform by hand once in that window if you
want its logged-in surfaces; no command reads, types or stores credentials.

```powershell
# whole chain, one board at a time, browser on for this shell only
.\scripts\live_run.ps1

# just the two hardest platforms, different track
.\scripts\live_run.ps1 -Boards linkedin,indeed -Profile swe

# everything except the browser and the network
.\scripts\live_run.ps1 -DryRun
```

Equivalent manual sequence:

```powershell
$env:LEADFORGE_BROWSER_ENABLED = "true"
leadforge doctor                       # reports the Tier-3 engine
leadforge e2e --profile ai_ml --board linkedin
leadforge e2e --profile ai_ml --board indeed
leadforge report --since 7
leadforge export-run-summary
```

If a challenge page appears, clear it in the visible window within
`LEADFORGE_BROWSER_MANUAL_CHALLENGE_WAIT_S` seconds (default 90). If it is not
cleared, the fetch fails and the host is paused like any other block. Nothing
solves challenges programmatically.

Set `LEADFORGE_BROWSER_HEADED=false` for an unattended run, and
`LEADFORGE_BROWSER_CHANNEL=` (empty) to use Playwright's bundled Chromium
instead of the installed Chrome.

## Proxy

One setting drives every tier:

```powershell
# .env  (never commit this file)
LEADFORGE_PROXY_URL=http://user:pass@gate.provider.net:8080
LEADFORGE_PROXY_TIER1=true
LEADFORGE_PROXY_TIER2=true
LEADFORGE_PROXY_TIER3=true
```

Accepted schemes: `http`, `https`, `socks5`, `socks5h`, `socks4`. A bare
`host:port` is treated as `http`. `leadforge doctor` prints the proxy host and
port only - never the password.

There is no silent fallback to the direct connection: if the proxy is set and
unreachable, the fetch fails. That is intentional - a job search that quietly
leaks your home IP defeats the point of paying for a residential proxy.

```powershell
# prove the proxy is in the path (should fail loudly)
$env:LEADFORGE_PROXY_URL = "http://127.0.0.1:9"
leadforge fetch https://example.com --text
```

Rotating proxies: most providers give either a rotating endpoint (one host, new
IP per request) or a sticky session in the username (`user-session-abc123`).
Use the sticky form when you need a logged-in LinkedIn session to keep the same
exit IP for the whole run.

## Email identity (a domain you own)

`leadforge domain-check` answers whether the domain can actually carry mail:

```powershell
leadforge domain-check                      # uses LEADFORGE_SMTP_MAIL_FROM
leadforge domain-check --domain outrajas.tech
```

It reports MX (can receive), SPF (can send), DMARC and DKIM, and prints the exact
records to add. At the registrar (name.com), the minimum set is:

| Type | Host | Value |
|---|---|---|
| MX | `@` | your mail provider (Cloudflare Email Routing or ImprovMX are free) |
| TXT | `@` | `v=spf1 include:<provider> ~all` |
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:you@yourdomain` |

Start DMARC at `p=none`. Raise to `p=quarantine` only after a month of clean
reports, or you will silently lose your own mail.

With a real identity configured, verification runs in `domain` mode:

```ini
LEADFORGE_SMTP_VERIFY_MODE=domain
LEADFORGE_SMTP_MAIL_FROM=verify@outrajas.tech
LEADFORGE_OPERATOR_NAME=Ojas
LEADFORGE_OPERATOR_EMAIL=ojas@outrajas.tech
```

`OPERATOR_*` is what appears as the sign-off in every generated draft, so the
manual send is consistent. The drafts never send themselves.
