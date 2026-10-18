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
