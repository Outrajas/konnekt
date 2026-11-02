"""Make a fresh checkout runnable: environment, directories, companies, checks.

The goal is that a new machine needs exactly two secrets (an LLM key and, if the
operator wants the big platforms, a proxy) and nothing else. Everything in this
module is idempotent — running it twice is a no-op — and it never overwrites a
file the operator has edited.
"""

from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from leadforge.config import Settings

# A starting point, not a target list: India-heavy AI/ML and product companies
# with domains that resolve. The operator is expected to edit this file.
STARTER_COMPANIES: tuple[tuple[str, str], ...] = (
    ("Sarvam AI", "sarvam.ai"),
    ("Krutrim", "krutrim.ai"),
    ("Fractal Analytics", "fractal.ai"),
    ("Quantiphi", "quantiphi.com"),
    ("Tiger Analytics", "tigeranalytics.com"),
    ("LatentView Analytics", "latentview.com"),
    ("Tredence", "tredence.com"),
    ("Sigmoid", "sigmoid.com"),
    ("Affine", "affine.ai"),
    ("Course5 Intelligence", "course5i.com"),
    ("Gramener", "gramener.com"),
    ("Innovaccer", "innovaccer.com"),
    ("Haptik", "haptik.ai"),
    ("Observe.AI", "observe.ai"),
    ("Uniphore", "uniphore.com"),
    ("Gupshup", "gupshup.io"),
    ("Yellow.ai", "yellow.ai"),
    ("Whatfix", "whatfix.com"),
    ("MoEngage", "moengage.com"),
    ("CleverTap", "clevertap.com"),
    ("Freshworks", "freshworks.com"),
    ("Zoho", "zoho.com"),
    ("Chargebee", "chargebee.com"),
    ("Postman", "postman.com"),
    ("BrowserStack", "browserstack.com"),
    ("Hasura", "hasura.io"),
    ("Atlan", "atlan.com"),
    ("Razorpay", "razorpay.com"),
    ("CRED", "cred.club"),
    ("PhonePe", "phonepe.com"),
    ("Paytm", "paytm.com"),
    ("Meesho", "meesho.com"),
    ("Swiggy", "swiggy.com"),
    ("Zomato", "zomato.com"),
    ("Urban Company", "urbancompany.com"),
    ("Lenskart", "lenskart.com"),
    ("Nykaa", "nykaa.com"),
    ("PolicyBazaar", "policybazaar.com"),
    ("Dream11", "dream11.com"),
    ("Games24x7", "games24x7.com"),
    ("ShareChat", "sharechat.com"),
    ("InMobi", "inmobi.com"),
    ("Mu Sigma", "mu-sigma.com"),
    ("Jupiter", "jupiter.money"),
    ("Fi Money", "fi.money"),
)


@dataclass
class SetupReport:
    created_env: bool = False
    created_companies: int = 0
    companies_path: str = ""
    missing: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        return not self.missing


def ensure_env_file(root: Path) -> bool:
    """Copy .env.free.example to .env when .env does not exist yet."""
    env = root / ".env"
    if env.exists():
        return False
    template = root / ".env.free.example"
    if not template.exists():
        return False
    shutil.copyfile(template, env)
    return True


def write_starter_companies(path: Path, *, force: bool = False) -> int:
    """Create (or extend) the operator companies file with the starter list."""
    existing: dict[str, tuple[str, str]] = {}
    if path.exists() and not force:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                name = (row.get("name") or "").strip()
                domain = (row.get("domain") or "").strip().lower()
                if name and domain:
                    existing[name.lower()] = (name, domain)

    added = 0
    for name, domain in STARTER_COMPANIES:
        if name.lower() not in existing:
            existing[name.lower()] = (name, domain)
            added += 1

    rows = sorted(existing.values(), key=lambda pair: pair[0].lower())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["name", "domain"])
        for name, domain in rows:
            writer.writerow([name, domain])
    return added


def missing_inputs(settings: Settings) -> list[str]:
    """What still needs the operator's hands, in the order they matter."""
    missing: list[str] = []
    if not settings.llm_base_url or not settings.llm_api_key or not settings.llm_model:
        missing.append(
            "LLM key: set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL "
            "(free tier or local Ollama at http://127.0.0.1:11434/v1)"
        )
    profile_db = settings.data_dir / "profile.json"
    if not profile_db.exists():
        missing.append("resume profile: leadforge profile ingest data/input/resume_*.pdf")
    companies = settings.input_dir / "companies.csv"
    if not companies.exists():
        missing.append("companies: leadforge setup (writes the starter list)")
    return missing


def optional_inputs(settings: Settings) -> list[str]:
    out: list[str] = []
    if not settings.proxy_url and not (settings.proxy_file and settings.proxy_file.exists()):
        out.append(
            "proxy (optional): only needed for LinkedIn/Indeed/Glassdoor — "
            "set LEADFORGE_PROXY_URL or run scripts/proxy_harvest.py"
        )
    if settings.smtp_verify_mode == "domain" and not settings.smtp_mail_from:
        out.append("SMTP: mode=domain needs LEADFORGE_SMTP_MAIL_FROM, or use null_sender")
    if not settings.browser_enabled:
        out.append("browser tier (optional): LEADFORGE_BROWSER_ENABLED=true for JS-heavy sites")
    return out


def run_setup(
    settings: Settings,
    *,
    root: Path | None = None,
    force_companies: bool = False,
) -> SetupReport:
    root = root or Path.cwd()
    report = SetupReport()
    report.created_env = ensure_env_file(root)

    companies = settings.input_dir / "companies.csv"
    report.created_companies = write_starter_companies(companies, force=force_companies)
    report.companies_path = str(companies)

    for directory in (settings.data_dir, settings.input_dir, settings.output_dir,
                      settings.cache_dir):
        directory.mkdir(parents=True, exist_ok=True)

    if report.created_env:
        report.notes.append(
            ".env created from .env.free.example - fill the LLM key, nothing else is required"
        )
    if report.created_companies:
        report.notes.append(f"{report.created_companies} starter companies written")

    report.missing = missing_inputs(settings)
    report.notes.extend(optional_inputs(settings))
    return report
