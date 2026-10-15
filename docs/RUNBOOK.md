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
