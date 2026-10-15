import tomllib
from pathlib import Path


def test_operator_approved_platform_policy() -> None:
    policy_path = Path(__file__).parents[1] / "data" / "domain_policy.toml"
    with policy_path.open("rb") as policy_file:
        policy = tomllib.load(policy_file)

    for domain in ("linkedin.com", "indeed.com", "glassdoor.com"):
        rule = policy[domain]
        assert rule["enabled"] is True
        assert rule["min_delay_s"] == 30.0
        assert rule["max_delay_s"] == 90.0
        assert rule["daily_cap"] == 10
        assert rule["max_tier"] == 1
        assert rule["allow_challenge_solving"] is False
