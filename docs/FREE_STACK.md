# The zero-budget stack

Everything the pipeline needs has a free path. This page says which, what it
costs you in reliability, and what was actually measured on this machine rather
than assumed.

Start from [`.env.free.example`](../.env.free.example).

---

## 1. LLM — free, with a fallback

| Provider | Free offer | OpenAI-compatible | Notes |
|---|---|---|---|
| OpenRouter | `:free` model variants | yes | ~20 req/min, per-model daily caps; the default choice |
| Groq | free tier | yes | very high tokens/sec, tight per-day limits |
| Google AI Studio (Gemini) | free tier | yes (`generativelanguage.googleapis.com/v1beta/openai`) | good at structured JSON |
| Cerebras | free tier | yes | fast, small daily quota |
| Mistral La Plateforme | free experiment tier | yes | quota resets monthly |
| **Ollama (local)** | free forever | yes (`http://127.0.0.1:11434/v1`) | no network, no quota, needs 8–16 GB RAM for a useful model |

The client now takes a **fallback provider**: if the primary returns 429/5xx
after its retries, or exhausts the daily cap, the next endpoint is tried and the
run continues instead of dying. Configure:

```ini
LLM_BASE_URL=...        LLM_API_KEY=...        LLM_MODEL=...
LLM_FALLBACK_BASE_URL=... LLM_FALLBACK_API_KEY=... LLM_FALLBACK_MODEL=...
```

Two levers keep you inside a free tier without touching code:
`LEADFORGE_LLM_REQUESTS_PER_MINUTE` and `LEADFORGE_LLM_DAILY_CAP`. The client
enforces both locally, before the provider can 429 you.

If you have the hardware, **Ollama is the only option with no quota at all** —
`qwen2.5:7b-instruct` or `llama3.1:8b` handle job-intel classification fine,
since the schema is small and the prompts are short.

---

## 2. Proxy — free is possible, free is weak

Measured on this machine (2026-11-01):

- `scripts/proxy_harvest.py` pulled **14,800+ candidates** from four public
  lists (TheSpeedX, monosans, proxifly).
- Of the **80** tested end-to-end against `api.ipify.org` and `example.com`,
  **0 survived**. Free lists are overwhelmingly dead, cloned, or already
  blocklisted; this is normal, not a bug.

So the realistic free options, best first:

| Option | Cost | Reality |
|---|---|---|
| **Cloudflare WARP** (`warp-cli mode proxy`, `socks5://127.0.0.1:40000`) | free | Changes your egress ASN to Cloudflare. Helps per-IP counters; Cloudflare IPs are themselves well-known |
| **Mobile hotspot / tethering** | free-ish | A genuine residential/mobile IP. Slow, but the best free reputation you can get |
| **Tor** (`socks5h://127.0.0.1:9050`) | free | Real IP diversity, but a large share of exit nodes are blocked outright by Cloudflare/LinkedIn |
| **Harvested free lists** | free | 0/80 usable in this sample. Worth re-running, not worth relying on |

What free proxies cannot do is substitute for residential IP reputation. If a
target blocks by ASN or IP history — Indeed, LinkedIn, Glassdoor — no free
option reliably gets through. The code handles that honestly: a configured dead
proxy **fails the fetch** rather than silently using your home IP.

Rotation is built in: `data/proxies.txt` (one URL per line, `#` comments) is
loaded as a pool, used round-robin by Tiers 1 and 2, and a proxy that fails is
parked for five selections before being retried.

```powershell
python scripts/proxy_harvest.py --limit 200 --workers 50   # refresh the pool
```

---

## 3. SMTP identity — free, no domain

Three modes, only one needs money:

| Mode | Needs | Free? |
|---|---|---|
| `off` | nothing | yes — pattern/A-tier evidence only, no SMTP probes |
| `null_sender` | nothing; probes with `MAIL FROM:<>` | **yes** — this is the free default |
| `domain` | a real address at a domain you control | no — ~₹800/yr for the domain |

`null_sender` is the standard non-sending verification trick and needs no
mailbox at all. Set `LEADFORGE_SMTP_VERIFY_MODE=null_sender` and skip the domain
purchase entirely.

Sending outreach stays manual either way — the project has no send path, by
design. That part costs nothing because you send from your normal mail client.

---

## 4. Search — free and keyless already

- **DuckDuckGo HTML** adapter: built in, no key.
- **SearXNG**: self-host with Docker (`docker run -p 8888:8080 searxng/searxng`),
  free, and the better option because it aggregates several engines:
  `LEADFORGE_SEARXNG_BASE_URL=http://127.0.0.1:8888`.

---

## 5. Boards — no credentials at all

RemoteOK, Remotive, Himalayas, We Work Remotely, Unstop, Instahyre, Hacker News,
YC, Welcome to the Jungle, Wellfound, Cutshort, Foundit, Hirist and Naukri are
public endpoints or plain HTML. **Zero API keys across all of them.**

LinkedIn, Indeed, Glassdoor and Naukri need a **signed-in browser session**, not
a key: you log in by hand once in the window Tier 3 opens, and
`data/browser-profile/` remembers it.

---

## 6. What still costs money, and whether you need it

| Item | Price | Need it? |
|---|---|---|
| Residential proxy | $3–15/GB | Only for LinkedIn/Indeed/Glassdoor at any volume. Skip until the free boards stop yielding |
| Domain | ~₹800/yr | Only for `smtp_verify_mode=domain`. Skip with `null_sender` |
| Paid unblocker (Zyte, Bright Data) | $$$ | Not needed; the code degrades instead |
| Hunter/Apollo/Clearbit | $$$ | Never needed — no part of the pipeline depends on them |

---

## 7. Honest expectations on a zero budget

- The **14 keyless boards work today** (121 jobs in the current database, 7
  sources yielding).
- **Email verification** works in `null_sender` mode, but catch-all domains will
  cap confidence at 0.40 regardless of who pays for what.
- **LinkedIn at scale is not free.** A headed browser on a WARP or hotspot IP
  can read a handful of pages; it will not survive hundreds. The policy caps
  (8–25 s spacing, 80–120/day) are the real limiter, not the stealth stack.
- **Tor will get you a captcha**, predictably. It is useful for the odd page,
  not for a crawl.
