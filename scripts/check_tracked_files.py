"""Fail when forbidden local data or recognizable API credentials are tracked."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

FORBIDDEN_KEY_PATTERNS = (
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?im)\b(?:OPENAI_)?API[_-]?KEY\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{16,}"),
)


def tracked_file_violations(root: Path, tracked_paths: list[str]) -> list[str]:
    violations = []
    for relative in tracked_paths:
        normalized = relative.replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        parts = normalized.split("/")
        forbidden_path = (
            normalized == ".env"
            or (normalized.startswith("data/input/") and normalized != "data/input/.gitkeep")
            or normalized == "data/profile.json"
            or normalized.startswith("data/output/")
            or parts[0] == "bin"
            or Path(normalized).name.lower().endswith(".db")
        )
        if forbidden_path:
            violations.append(f"{relative}: forbidden tracked path")
            continue
        path = root / relative
        try:
            content = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(pattern.search(content) for pattern in FORBIDDEN_KEY_PATTERNS):
            violations.append(f"{relative}: possible API key")
    return violations


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    tracked = (
        subprocess.run(["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True)
        .stdout.decode("utf-8")
        .split("\0")
    )
    violations = tracked_file_violations(root, [path for path in tracked if path])
    if violations:
        print("\n".join(violations))
        return 1
    print("Tracked-file hygiene check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
