# leadforge

Self-hosted email enrichment pipeline for job-search outreach.

## Quickstart (Windows PowerShell)

The operator uses `.\.venv`; the agent uses `.\.venv-agent`. The agent environment is a workspace copy configured for the Codex Python runtime.

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
leadforge enrich tests/fixtures/sample_contacts.csv --replay data/fixtures/sample --force
leadforge report --since 30
leadforge people --company Acme --domain acme.invalid --replay data/fixtures/people/sample
```

`leadforge verify` and live `leadforge enrich` require a real
`LEADFORGE_SMTP_MAIL_FROM` only when `LEADFORGE_SMTP_VERIFY_MODE=domain`. Live
discovery, people search, walk, and enrichment also require the operator's
`data/input/companies.csv` and an ingested resume profile. Preview network work
with each command's `--dry-run` option. All web requests go through the policy
router; no command sends email.

For optional live people search, configure `LEADFORGE_SEARXNG_BASE_URL` to use
local SearxNG; otherwise the logged-out DuckDuckGo adapter parses search
snippets through the policy router. Search result URLs are never fetched.

The tier benchmark requires an operator-owned `data/input/benchmark_domains.txt`.
Inspect its request plan first with:

```powershell
python scripts/benchmark_tiers.py --domains data/input/benchmark_domains.txt --dry-run
```

The live benchmark has not been run.
