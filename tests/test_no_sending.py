import re
from pathlib import Path


def test_production_source_has_no_email_sending_path() -> None:
    patterns = (
        re.compile(r"\bsendmail\s*\(", re.IGNORECASE),
        re.compile(r"\bsend_message\s*\(", re.IGNORECASE),
        re.compile(r"gmail\.users\.messages\.send", re.IGNORECASE),
        re.compile(r"\bDATA\b"),
    )
    violations = []
    for path in Path("src/leadforge").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(source.splitlines(), start=1):
            code = line.split("#", maxsplit=1)[0]
            if any(pattern.search(code) for pattern in patterns):
                violations.append(f"{path}:{line_number}: {line.strip()}")

    assert not violations, "Production email-sending path detected:\n" + "\n".join(violations)
