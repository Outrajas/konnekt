# leadforge

Self-hosted email enrichment pipeline for job-search outreach.

## Quickstart (Windows PowerShell)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
Copy-Item .env.example .env
leadforge doctor
```

## Offline demo

The committed demo inputs are fictional and use the reserved `.invalid` domain.
Run the end-to-end replay without network, SMTP, or LLM calls:

```powershell
leadforge enrich data/input/sample_contacts.csv --replay data/fixtures/sample --force
leadforge report --since 30
leadforge people --company Acme --domain acme.invalid --replay data/fixtures/people/sample
```

`leadforge verify` and live `leadforge enrich` require
`LEADFORGE_SMTP_MAIL_FROM` to be a real address at a domain you control. Live
discovery, people search, walk, and enrichment also require the operator's
`data/input/companies.csv` and an ingested resume profile. Preview network work
with each command's `--dry-run` option. All web requests go through the policy
router; no command sends email.

For optional live people search, set `LEADFORGE_SEARXNG_BASE_URL` to your local
SearxNG instance. The DuckDuckGo adapter is waiting on a real response snapshot
after operator inputs are supplied.
