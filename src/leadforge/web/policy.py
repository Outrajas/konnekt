import logging
import tomllib
from dataclasses import dataclass
from numbers import Real
from pathlib import Path

import tldextract

from leadforge.config import Settings

log = logging.getLogger(__name__)
_extract_domain = tldextract.TLDExtract(suffix_list_urls=())


@dataclass(frozen=True)
class DomainRule:
    enabled: bool = True
    min_delay_s: float = 3.0
    max_delay_s: float = 8.0
    daily_cap: int = 30
    max_tier: int = 2
    obey_robots: bool = True
    allow_challenge_solving: bool = False


class DomainPolicy:
    def __init__(self, rules: dict[str, DomainRule], default: DomainRule):
        self.rules = rules
        self.default = default

    @classmethod
    def load(cls, path: Path, settings: Settings | None = None) -> "DomainPolicy":
        settings = settings or Settings()
        if not path.exists():
            log.warning("Domain policy file missing; using settings defaults: %s", path)
            return cls(
                {},
                DomainRule(
                    min_delay_s=settings.default_domain_min_delay_s,
                    max_delay_s=settings.default_domain_max_delay_s,
                    daily_cap=settings.default_domain_daily_cap,
                ),
            )
        try:
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValueError(f"Invalid domain policy file {path}: {exc}") from exc
        settings_default = DomainRule(
            min_delay_s=settings.default_domain_min_delay_s,
            max_delay_s=settings.default_domain_max_delay_s,
            daily_cap=settings.default_domain_daily_cap,
        )
        default = _rule(raw.get("default", {}), settings_default)
        rules = {
            host.lower(): _rule(values, default)
            for host, values in raw.items()
            if host != "default" and isinstance(values, dict)
        }
        return cls(rules, default)

    def resolve(self, host: str) -> DomainRule:
        host = host.lower().rstrip(".")
        if host in self.rules:
            return self.rules[host]
        extracted = _extract_domain(host)
        registrable = extracted.top_domain_under_public_suffix or host
        return self.rules.get(registrable, self.default)

    def budget_key(self, host: str) -> str:
        """Share caps and pauses across subdomains with an explicit platform rule."""
        host = host.lower().rstrip(".")
        extracted = _extract_domain(host)
        registrable = extracted.top_domain_under_public_suffix or host
        return registrable if registrable in self.rules else host


def _rule(values: dict[str, object], base: DomainRule) -> DomainRule:
    allowed = DomainRule.__dataclass_fields__
    selected = {key: values.get(key, getattr(base, key)) for key in allowed}
    try:
        rule = DomainRule(**selected)  # type: ignore[arg-type]  # keys/types validated below
    except TypeError as exc:
        raise ValueError(f"Invalid domain policy rule: {exc}") from exc
    if not isinstance(rule.enabled, bool) or not isinstance(rule.obey_robots, bool):
        raise ValueError("Domain policy enabled and obey_robots values must be booleans")
    if not isinstance(rule.allow_challenge_solving, bool):
        raise ValueError("Domain policy allow_challenge_solving must be a boolean")
    if (
        isinstance(rule.min_delay_s, bool)
        or isinstance(rule.max_delay_s, bool)
        or not isinstance(rule.min_delay_s, Real)
        or not isinstance(rule.max_delay_s, Real)
        or isinstance(rule.daily_cap, bool)
        or not isinstance(rule.daily_cap, int)
        or isinstance(rule.max_tier, bool)
        or not isinstance(rule.max_tier, int)
    ):
        raise ValueError("Domain policy delay, daily cap, or tier value has the wrong type")
    if rule.min_delay_s < 0 or rule.max_delay_s < rule.min_delay_s:
        raise ValueError("Domain policy delays must satisfy 0 <= min_delay_s <= max_delay_s")
    if rule.daily_cap < 0 or rule.max_tier not in (0, 1, 2, 3):
        raise ValueError("Domain policy daily_cap must be nonnegative and max_tier between 0 and 3")
    return rule
