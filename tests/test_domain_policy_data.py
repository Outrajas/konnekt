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

    wellfound = policy["wellfound.com"]
    assert wellfound["enabled"] is True
    assert wellfound["min_delay_s"] == 30.0
    assert wellfound["max_delay_s"] == 90.0
    assert wellfound["daily_cap"] == 10
    assert wellfound["max_tier"] == 1
    assert wellfound["allow_challenge_solving"] is False

    startup = policy["workatastartup.com"]
    assert startup["enabled"] is True
    assert startup["min_delay_s"] == 5.0
    assert startup["max_delay_s"] == 15.0
    assert startup["daily_cap"] == 40
    assert startup["max_tier"] == 2
    assert startup["allow_challenge_solving"] is False

    default_challenge_solving = policy["default"]["allow_challenge_solving"]
    assert all(
        rule.get("allow_challenge_solving", default_challenge_solving) is False
        for name, rule in policy.items()
        if name != "default"
    )
    assert all(
        policy[domain]["max_tier"] == 1
        for domain in ("linkedin.com", "indeed.com", "glassdoor.com")
    )
