<!--
Place at: docs/SPEC.md
This is the original v0.1.0 specification exactly as written by the operator.
Operator-approved deviations are tracked in docs/SPEC_DELTA.md. Do not edit this
file; propose changes in SPEC_DELTA.md and the operator will merge them here.
-->

# leadforge — Project Specification

**Repository:** `https://github.com/Outrajas/majdoori`
**Version:** 0.1.0 (scaffold)
**Target environment:** Windows PowerShell 5.1, Python 3.12, local-first (no cloud dependencies required for core function)
**Author context:** Sole operator, job-seeking, targeting India + remote-global roles

---

## 1. The Problem

Traditional job applications are a losing game. The pipeline is:

> Submit resume → ATS filters by keyword → (rarely) human reads → (rarely) callback

Conversion rates sit at 1–3% interview yield per cold application. The ATS is not a person — it is a keyword matcher that rejects 75%+ of applicants before any human sees them. Meanwhile, the candidates who *do* get interviews are disproportionately the ones who found a human on the other side and talked to them directly.

The operator of this project has already built (and scrapped) one attempt at solving this: scrapers for LinkedIn, Indeed, Glassdoor, Naukri, and Unstop; an HR finder; an identity graph experiment; an LLM-based email generator. It worked partially, then got banned — headless browsers fingerprinted, TLS handshakes flagged, IPs rate-limited, sessions killed.

**The core insight driving this rebuild:** the bottleneck is not job discovery. It is **finding the right human at the right company and reaching them in a way that survives spam filters and doesn't read as stalking**. Everything else — the scraping, the parsing, the LLM drafting — is downstream of that single hard problem.

---

## 2. The Vision

Build a **local-first, self-hosted enrichment pipeline** that:

1. **Discovers** companies and roles matching the operator's scope (defined by resume, skills, location, and employment type)
2. **Finds** specific humans at those companies — HR, technical managers, team leads — via open-web OSINT, not paid APIs
3. **Derives** their likely professional email addresses through pattern inference, web evidence, and SMTP verification
4. **Generates** personalized outreach that references the person's public professional work (a talk, a repo, a post) and never crosses into personal-info territory
5. **Sends** with human approval at every step — no bulk auto-send, no spam
6. **Avoids** fingerprinting, rate-limiting, and bot-labelling through a layered anti-detection stack

The pipeline must be **auditable** — every email candidate carries its full provenance (which tool generated it, whether it was found on the web, how it was verified, what its confidence score is). It must **fail safe** — returning "not found" rather than a guessed address. It must be **operable by one person on Windows** without a DevOps background.

**Explicit non-goals for v1:**
- No machine learning / self-improving pattern model (deferred to v2)
- No bulk auto-send (human approval is non-negotiable)
- No paid APIs (Hunter, Apollo, Clay, or equivalent)
- No cloud hosting (local-first; a VPS is acceptable later)

---

## 3. Architecture

The pipeline is five stages, connected linearly. Each stage is a separate module under `src/leadforge/stages/`. Each external tool is wrapped under `src/leadforge/tools/`.

```
┌─────────────────────────────────────────────────────────────────┐
│                     STAGE 1: DISCOVERY                          │
│  Input:  operator profile (resume PDF, skills, scope)           │
│  Output: list of (company, role, job_posting_url)               │
│  Tools:  jobspy-js (LinkedIn/Indeed/Glassdoor/Google Jobs)      │
│          Scrapling Spider for company career pages              │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                   STAGE 2: CONTACT EXTRACTION                   │
│  Input:  company domain or name                                 │
│  Output: list of (first_name, last_name, title, linkedin_url)   │
│  Tools:  Google dorks (site:linkedin.com/in "Company")          │
│          Social Analyzer / Maigret for cross-platform identity  │
│          FORGE for local business-record enrichment             │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                   STAGE 3: EMAIL DISCOVERY                      │
│  Input:  (first_name, last_name, domain)                        │
│  Output: ranked list of EmailCandidate objects                  │
│  Tools:  email-permutations (pattern generation)                │
│          Web crawl of company contact/about/team pages          │
│          Optional: email-sleuth / ColdReach for cross-check     │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                     STAGE 4: VERIFICATION                       │
│  Input:  EmailCandidate list                                    │
│  Output: EmailCandidate list with verdict + confidence          │
│  Tools:  dnspython (MX lookup)                                  │
│          aiosmtplib (RCPT TO handshake, no email sent)          │
│          Catch-all domain detection                             │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                      STAGE 5: OUTPUT                            │
│  Input:  verified EmailCandidate list                           │
│  Output: CSV/JSON with name, email, confidence, provenance      │
│  Then:   manual review → LLM-assisted draft → human sends       │
└─────────────────────────────────────────────────────────────────┘
```

**The anti-detection layer is orthogonal** — it wraps Stages 1 and 3 (the web-touching stages). It is not a stage itself; it is a property of how the tools fetch.

---

## 4. The Anti-Detection Stack: Scrapling + Obscura

This is the part that killed the previous attempt. Getting blocked wasn't a parsing problem — it was a fingerprinting problem. Two tools, used together, cover more of the detection surface than either alone.

### 4.1 The five detection layers

Anti-bot systems check five layers. Each tool covers a different subset:

| Layer | What it checks | Scrapling | Obscura |
|---|---|---|---|
| **1. Protocol** | CDP timing, `Runtime.enable` leaks | ✅ Patchright-based stealth | ✅ CDP with no Chromium leaks |
| **2. Fingerprint** | Canvas, WebGL, audio, screen | ✅ JS injection + noise | ✅ Consistent Chrome 145 identity across TLS/JS/WebGL |
| **3. Behavioural** | Mouse curves, timing | ⚠️ Partial | ❌ Not built-in |
| **4. Network (TLS)** | JA3/JA4 handshake | ✅ `curl_cffi` impersonation | ✅ `wreq` with browser-matching ClientHello |
| **5. Layout** | Rendering quirks | ✅ Real Chromium | ✅ Native Rust renderer |

**Neither tool covers all five perfectly.** The comparison literature is explicit: "IP reputation and TLS matter more than fingerprint polish". That means proxies + TLS impersonation are the foundation, and fingerprint consistency is the polish on top.

### 4.2 Scrapling — the orchestration layer

Scrapling is an **all-in-one Python framework** — fetching, parsing, anti-detection, crawling, and AI integration in one package. It offers three fetchers:

- **`Fetcher`** — HTTP-only, fast, TLS-impersonating via `curl_cffi`. Best for static pages, APIs, RSS. No JavaScript.
- **`DynamicFetcher`** — full Playwright/Chromium. Renders JavaScript. Heavier, slower, more detectable.
- **`StealthyFetcher`** — Patchright-based stealth Chromium. Canvas noise, WebRTC blocking, 60+ stealth flags, Cloudflare Turnstile solving.

Its **adaptive parser** is the unique feature: `p.css('.product', auto_save=True)` records the element's properties. If the site redesigns, `p.css('.product', adaptive=True)` relocates it by similarity — scrapers survive layout changes without code edits.

Its **spider framework** is Scrapy-like: `start_urls`, async `parse()`, concurrency limits, pause/resume, proxy rotation, AutoThrottle that backs off when the target starts blocking.

Its **MCP server** exposes 6–11 tools for AI agents (Claude Code, Cursor, etc.) — conversational scraping.

**Where Scrapling is the right tool:**
- Cloudflare Turnstile-protected targets (LinkedIn, some Indeed endpoints)
- Any target where the DOM changes and you want the scraper to survive
- Full crawls with concurrency, retries, and pause/resume
- When you want a single Python library doing the whole pipeline

**Where Scrapling is overkill or wrong:**
- High-volume fetching of 500+ simple company contact pages (StealthyFetcher at ~200MB per browser context is expensive)
- Targets that don't need a full Chromium (binary is large, startup is ~2s)

### 4.3 Obscura — the lightweight rendering backend

Obscura is a **from-scratch Rust headless browser engine** — no Chromium dependency, ~70MB binary, ~30MB RAM at runtime, instant cold start. It runs real JavaScript via V8 and speaks the Chrome DevTools Protocol, so Puppeteer and Playwright can connect to it via `connect_over_cdp`.

Its stealth mode (`--stealth`) does three things:
- Uses `wreq` HTTP client with **browser-matching TLS fingerprints** (ClientHello, ALPN, cipher order)
- Loads a **tracker blocklist** (3,520 domains) that drops analytics and fingerprinting endpoints
- Presents a **single consistent Chrome 145 identity** across TLS, JS, and WebGL surfaces — creepjs reports 0% stealth detection

It also has a **batch scrape mode**: `obscura scrape url1 url2 ... --concurrency 10` — parallel JS evaluation across URLs, output as JSON.

**Where Obscura is the right tool:**
- Batch fetching 100+ company contact pages in parallel (30MB per page vs 200MB for Chrome)
- Consistent-identity stealth where you don't need Cloudflare solving
- When you want a CDP endpoint for Puppeteer/Playwright but don't want to ship Chromium
- Screenshots, PDFs, screencasts — Obscura renders natively

**Where Obscura is wrong:**
- **It explicitly does not handle** Cloudflare interactive challenges, DataDome, Akamai active challenges, CAPTCHAs, or IP-based rate limiting
- No adaptive parsing — you parse the HTML yourself
- No spider framework — you orchestrate the batch yourself
- Stealth mode is not in the Docker image; you must build from source with `--features stealth`

### 4.4 The hybrid architecture

The two tools are not competitors. They fill different gaps:

> **Obscura fills the gap between Lightpanda (CDP, no stealth) and Scrapling (stealth, no CDP).**

The comparison literature names Scrapling as the tool for "a full pipeline that ties several tools together". That is the role it plays here.

| Task | Primary tool | Why |
|---|---|---|
| **Job board scraping** (LinkedIn, Indeed, Glassdoor) | Scrapling `StealthyFetcher` | Needs Cloudflare solving + session persistence + proxy rotation |
| **Batch company contact pages** (100s of `example.com/contact`) | Obscura `scrape` | 30MB/page, parallel, consistent identity, fast |
| **Adaptive parsing** (site redesigns) | Scrapling parser | Obscura has no adaptive parser |
| **CDP-driven automation** (login flows, form fills) | Obscura `serve` + Playwright | Scrapling's CDP is limited; Obscura is a native CDP server |
| **High-volume proxy rotation** | Scrapling `ProxyRotator` | Built into the spider framework; Obscura only takes `--proxy` per invocation |
| **MCP / AI-agent orchestration** | Either | Both expose MCP servers; pick per agent |

**Integration pattern:** Scrapling is the orchestrator. When a target requires Cloudflare solving or adaptive parsing, Scrapling's `StealthyFetcher` runs in-process. When a target is a plain company page that needs to be fetched 200 times in parallel, Scrapling's spider hands the URL list to Obscura's `scrape` subprocess, gets JSON back, and parses it with Scrapling's `Selector`. The two tools never fight — they hand off.

**Proxy layer:** Both support HTTP, HTTPS, and SOCKS5 proxies. Scrapling's `ProxyRotator` is managed at the spider level; Obscura takes a `--proxy` flag per invocation. For v1, use **residential proxies in the same geographic region as the target** — the literature is explicit that IP reputation beats fingerprint polish. Match timezone (`OBSCURA_TIMEZONE`) and geolocation (`OBSCURA_GEOLOCATION`) to the proxy's region, or the mismatch is itself a signal.

**What this stack cannot do:** DataDome, Akamai, Kasada, and Incapsula active challenges are outside both tools' native capabilities. If a target uses those, the options are (a) a paid unlocker API, or (b) skip the target. For a personal job search at human scale, that is acceptable.

---

## 5. Data Model

Three core Pydantic models, already written into `src/leadforge/models.py`:

**`Contact`** — a person we're trying to reach.
- `first_name`, `last_name`, `domain` (required)
- `company`, `title`, `linkedin_url`, `source` (optional)

**`EmailCandidate`** — one candidate email for one contact, with full provenance.
- `email`, `contact`, `source` (required)
- `pattern` (e.g. `"first.last"`, `"flast"`)
- `found_on_web` — was this scraped from a real page, or generated?
- `catch_all_domain` — does the domain accept all mail?
- `verification_method` — `smtp`, `api`, `browser`, `pattern_only`, `unverified`
- `verification_verdict` — `send`, `hold`, `kill`, `unknown`
- `confidence` — float 0.0–1.0
- `notes` — free text for debugging

**`EnrichmentResult`** — the final output for one contact.
- `contact`, `best_email`, `all_candidates`, `enriched_at`

The design principle: **one contact can have many candidates; one candidate carries its own provenance.** Never collapse to a single email without recording what was discarded and why.

---

## 6. Confidence Scoring Rules

Each `EmailCandidate.confidence` is computed from:

| Signal | Weight | Rationale |
|---|---|---|
| SMTP verdict = `send` | +0.40 | The mail server accepted the address |
| Found on web (`found_on_web=True`) | +0.25 | Real evidence beats pattern inference |
| Source count ≥ 2 (multiple tools agree) | +0.15 | Independent confirmation |
| Pattern matches domain's other verified emails | +0.10 | Consistency signal |
| Catch-all domain (`catch_all_domain=True`) | −0.20 | SMTP verdict is unreliable here |
| Pattern-only, unverified | cap at 0.40 | Never emit an unverified guess above this |

**Emit threshold:** `settings.min_confidence_to_emit` (default 0.40). Anything below is logged but not surfaced for outreach.

---

## 7. Verification Rules (Stage 4)

SMTP verification must be **non-sending**. The sequence:

1. **Syntax check** — RFC 5322 pattern
2. **MX lookup** — `dnspython.resolver.resolve(domain, 'MX')`. No MX = domain can't receive mail = kill
3. **Connect to MX host** on port 25 (or 587/465 if 25 is blocked by the ISP)
4. **`MAIL FROM:<verify@yourdomain.com>`** — the operator's own domain, configured in `.env`
5. **`RCPT TO:<candidate@target.com>`** — the actual check
6. **Read the response code:**
   - `250` → `send` (mailbox exists)
   - `550` / `551` / `553` → `kill` (mailbox doesn't exist)
   - `450` / `451` / `452` → `hold` (temporary failure, retry later)
   - Anything else → `unknown`
7. **`QUIT`** — never `DATA`, never `RSET`-and-send

**Catch-all detection:** Send `RCPT TO:<random-12-char-string@target.com>`. If it returns `250`, the domain is catch-all. Cap confidence at 0.40 for all candidates at that domain — SMTP verdicts are meaningless there.

**Port 25 blocking:** Many ISPs and cloud hosts block outbound port 25. `aiosmtplib` handles 587 (STARTTLS) and 465 (implicit SSL) transparently. Fall back in that order. If all three are blocked, mark candidates `pattern_only` and let the operator verify manually via a browser.

**Rate limiting:** Space SMTP checks by `settings.request_delay_seconds` (default 1.0s). Never parallelise more than `settings.smtp_max_concurrent` (default 5) per target domain — mail servers flag connection bursts as spam probes.

---

## 8. Ethical and Legal Boundaries

These are not suggestions. They are constraints the code enforces.

**What the pipeline may do:**
- Scrape publicly accessible pages (company sites, public LinkedIn profiles, public forums)
- Derive email patterns from a name + domain
- SMTP-verify addresses without sending mail
- Generate outreach drafts for human review

**What the pipeline may not do:**
- Access any page behind a login wall it hasn't been authorized for
- Use the operator's own LinkedIn session to scrape at scale (personal use at human scale is the outer bound)
- Send email without explicit per-message human approval
- Reference personal information (marital status, location, unrelated social posts) in outreach
- Collect or store personal data beyond what's needed for the current search

**The stalking line:** Outreach references **public professional output** — a conference talk, a GitHub repo, a published article, a company blog post. It never references anything the person hasn't chosen to make public in a professional context. The code enforces this by tagging every evidence source with a `professional: bool` flag; anything `professional=False` is excluded from LLM prompts.

**GDPR / data protection:** B2B contact data (name, title, work email) collected for legitimate interest in a personal job search is generally permissible, but storing it long-term without a lawful basis is not. The pipeline should default to ephemeral storage — enrich, use, discard.

---

## 9. Operational Constraints

- **Platform:** Windows PowerShell 5.1, Python 3.12 (pinned — 3.14 is too new for stable wheels)
- **Encoding:** All generated files must be UTF-8 **without BOM**. PowerShell's `Set-Content -Encoding UTF8` adds a BOM that breaks Python's `tomllib`. Use `[System.IO.File]::WriteAllText($path, $content, (New-Object System.Text.UTF8Encoding $false))` for any file a Python parser will read.
- **No paid APIs.** Every tool in the stack is open-source or has a free tier sufficient for personal-scale use.
- **No cloud dependency for core function.** The pipeline must run entirely on the operator's machine. A VPS is acceptable for the anti-detection layer later, but not required.
- **Auditability.** Every output row must trace back to its source. If a candidate email can't explain where it came from, it doesn't get emitted.

---

## 10. Current State

**Built:**
- Project scaffold (`src/leadforge/`, `data/`, `tests/`, `scripts/`)
- `pyproject.toml` with dependencies: pydantic, pydantic-settings, typer, rich, httpx, aiosmtplib, dnspython, python-dotenv
- `config.py` — `Settings` class with env-var loading
- `models.py` — `Contact`, `EmailCandidate`, `EnrichmentResult`
- `cli.py` — `leadforge doctor` and `leadforge version`
- Venv created with Python 3.12.10, package installed editable
- Git remote wired to `https://github.com/Outrajas/majdoori`, first commit pushed

**Not yet built:**
- `tools/smtp_verify.py` — MX lookup + SMTP RCPT TO verification (Stage 4)
- `tools/email_patterns.py` — permutation generation from name + domain (Stage 3)
- `tools/scrapling_wrapper.py` — thin wrapper around Scrapling's fetchers
- `tools/obscura_wrapper.py` — subprocess wrapper around Obscura's `scrape` CLI
- `stages/discovery.py` — job discovery (Stage 1)
- `stages/contacts.py` — Google dorks + Social Analyzer (Stage 2)
- `stages/email_discovery.py` — orchestrates pattern gen + web crawl (Stage 3)
- `pipeline.py` — runs all stages over a CSV of contacts
- CLI commands: `leadforge enrich`, `leadforge verify`, `leadforge discover`

---

## 11. Immediate Next Step

`src/leadforge/tools/smtp_verify.py` — the verification module. Everything downstream depends on this being correct. It must:

- Accept a `Contact` and a list of `EmailCandidate` objects
- Resolve MX records via `dnspython`
- Attempt SMTP handshake via `aiosmtplib` on 25 → 587 → 465
- Return `VerificationVerdict` for each candidate
- Detect catch-all domains
- Respect rate limits and concurrency caps
- Never send mail
- Never log credentials

Once that module is tested against known-good and known-bad addresses, Stage 3 (email discovery) builds on top of it. Stage 5 (output) is trivial after that. Stages 1 and 2 are discovery — they generate the input list, but the pipeline works with a manually-supplied CSV from day one.

---

*This document is the contract. Any code that contradicts it is wrong, and the document wins. Update the document when the architecture changes — not the code.*
