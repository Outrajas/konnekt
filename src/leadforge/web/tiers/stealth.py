from leadforge.web.errors import TierUnavailable
from leadforge.web.policy import DomainRule


def require_allowed(rule: DomainRule) -> None:
    if not rule.allow_challenge_solving or rule.max_tier < 3:
        raise TierUnavailable("Tier 3 is disabled by domain policy")
    raise TierUnavailable("Tier 3 is intentionally disabled in this baseline")
