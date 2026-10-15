# Leadforge runbook

Run commands from the repository root in PowerShell after activating `.venv`.

```powershell
.\.venv\Scripts\Activate.ps1
leadforge version
leadforge doctor
leadforge init-db
```

`doctor` reports local configuration, tool availability, daily platform budgets, and the port-25 probe result with a remedy. `init-db` creates or migrates the local SQLite database.

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

Before running `leadforge verify` or `leadforge enrich`, set
`LEADFORGE_SMTP_MAIL_FROM` to a valid address at a domain the operator controls.
The verifier uses this value only for the SMTP `MAIL FROM` command; it never
sends message data. An empty value or the sample placeholder is refused.

## Discovery inputs

Place the operator resume in `data/input`, run `leadforge profile ingest <resume>`
and review `data/profile.json`. Add only operator-approved companies to
`data/input/companies.csv` (`name,domain`). Preview the routed requests with
`leadforge discover --companies data/input/companies.csv --dry-run`; a live run
is refused until the editable profile exists. Use `--include-hn` to add the
Hacker News hiring thread source.

People search works offline with `leadforge people --company Acme --domain
acme.example --replay data/fixtures/people/sample`. Live search requires the
company list, an ingested profile, and `LEADFORGE_SEARXNG_BASE_URL`; only result
snippets are parsed, and result pages are never fetched. DuckDuckGo support is
not enabled until a real response snapshot can be recorded after operator input
is available.

## Replay, review, and maintenance

Run the local demo with `leadforge enrich data/input/sample_contacts.csv
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

## Tier benchmark

Create `data/input/benchmark_domains.txt` with operator-approved domains, one
per line. Preview policy and budget scope with `python
scripts/benchmark_tiers.py --domains data/input/benchmark_domains.txt --dry-run`.
The live benchmark uses the router and may request Tier 2 only when the
configured Obscura binary exists; review the domain list before running it.
