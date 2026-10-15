import asyncio
from dataclasses import dataclass

from leadforge.web.errors import FetchFailed, TierUnavailable


@dataclass
class RawResponse:
    url: str
    status: int
    headers: dict[str, str]
    body: str


def build_argv(executable: str, url: str, timeout: int, *, stealth: bool) -> list[str]:
    argv = [executable, "--quiet", "fetch", url, "--dump", "html", "--timeout", str(timeout)]
    if stealth:
        argv.append("--stealth")
    return argv


async def fetch(
    url: str, *, executable: str | None, stealth: bool, timeout_seconds: int
) -> RawResponse:
    if not executable:
        raise TierUnavailable("Tier 2 is unavailable until OBSCURA_PATH is set")
    process = await asyncio.create_subprocess_exec(
        *build_argv(executable, url, timeout_seconds, stealth=stealth),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=timeout_seconds + 10
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise FetchFailed(f"Obscura timed out after {timeout_seconds + 10}s") from exc
    if process.returncode != 0 or not stdout:
        detail = stderr.decode("utf-8", errors="replace")[:500]
        raise FetchFailed(f"Obscura failed with exit {process.returncode}: {detail}")
    return RawResponse(url, 200, {}, stdout.decode("utf-8", errors="replace"))
