# Quickstart — zero to drafts

This is the whole thing: two inputs, one command.

## 1. The two inputs

Put these in `.env` (created for you by `leadforge setup`):

```ini
# 1) an LLM. Free options: OpenRouter ":free" models, Groq, Gemini,
#    or a local Ollama server (no key at all):
#    LLM_BASE_URL=http://127.0.0.1:11434/v1   LLM_MODEL=qwen2.5:7b-instruct
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=

# 2) a proxy — only needed for LinkedIn / Indeed / Glassdoor.
#    Free options: Cloudflare WARP (socks5://127.0.0.1:40000),
#    Tor (socks5h://127.0.0.1:9050), a phone hotspot, or
#    data/proxies.txt from `python scripts/proxy_harvest.py`.
LEADFORGE_PROXY_URL=
```

Everything else has a working default: `smtp_verify_mode=null_sender` (no
domain needed), DuckDuckGo search (keyless), the 14 keyless job boards, and a
45-company starter list.

## 2. Install

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,browser]"
playwright install chromium
```

## 3. One command

```powershell
leadforge setup          # .env, dirs, starter companies, tells you what is missing
leadforge doctor         # python, port 25, proxy, tier-3 engine
leadforge run-all        # resolve domains -> discover -> research -> people -> email -> draft -> review
```

`run-all` prints a stage table and writes `data/output/review_<timestamp>.csv`.
Research is enabled by default; use `--no-research` when you only want the
legacy deterministic people path.

For a visible, step-by-step operator run, use the PowerShell launcher:

```powershell
.\scripts\live_enrichment.ps1
```

It enables the headed browser profile, streams stages, search queries, result
URLs, fetched pages, person/contact clues, and stop reasons, then prints the
stored evidence report. It never invokes a send command. Use
`-Profile ai_ml -MaxCompanies 5` to change the scope or `-DryRun` to exercise
the orchestration without network requests.
Drafts land in `data/output/drafts/`. **Nothing is sent** — you read the review
row, approve, send by hand, then:

```powershell
leadforge mark-sent <person_id> <email>
leadforge today                      # what is eligible now (5/day ramping to 20)
leadforge mark-outcome <person_id> <email> reply
```

For a bounded public-web research trajectory on one company/job, use:

```powershell
leadforge research --company "Acme" --domain acme.example --role "ML Engineer"
leadforge research-all --max-companies 10
```

The research stage searches SearXNG when configured, otherwise DuckDuckGo,
follows selected public result links through the normal policy router, lets the
configured LLM propose additional queries, and records queries, hits, fetched
pages, clues, explicit website/person/contact edges, budgets, and the stop
reason in `research_run`, `research_event`, and `research_edge`. It persists
discovered people and published email evidence,
but never sends mail. Use `--dry-run` to skip network access and persistence.

## What each stage needs

| Stage | Input it uses | If missing |
|---|---|---|
| resolve-domains | `data/input/companies.csv` | skipped; board rows keep placeholders |
| discover | `companies.csv` + `/careers` pages | falls back to boards only |
| people | `/team` pages, DuckDuckGo snippets | 0 people, everything downstream 0 |
| email | person + real domain + SMTP | pattern-only candidates at 0.40 cap |
| draft | person + email + LLM | `NEEDS_MANUAL_PERSONALIZATION` drafts |
| review | drafts | no CSV |

## Useful flags

```powershell
leadforge run-all --dry-run                 # no network, no LLM, no writes
leadforge run-all --max-companies 5 --profile swe
leadforge run-all --no-resolve --no-search  # fastest, relies on /team pages only
leadforge discover --board linkedin         # platforms need the browser + proxy
leadforge resolve-domains --limit 50 --dry-run
```

## If people stays at 0

That is the honest bottleneck, in order of cost:

1. The company has no `/team` page and DuckDuckGo has no indexed profile.
2. Save a LinkedIn profile page by hand (logged out) into `data/input/saved_pages/`
   and run `leadforge import-pages` — fully offline.
3. Run with the browser tier and a proxy: `LEADFORGE_BROWSER_ENABLED=true`,
   then `leadforge e2e --board linkedin`.
