from pathlib import Path

from leadforge.web.errors import DeniedHost


def load_denylist(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.split("#", 1)[0].strip().lower().lstrip("*.")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip()
    }


def check_host(host: str, denylist: set[str]) -> None:
    host = host.lower().rstrip(".")
    if any(host == blocked or host.endswith("." + blocked) for blocked in denylist):
        raise DeniedHost(f"Host is on the denylist: {host}")
