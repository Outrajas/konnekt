from typing import Literal

Classification = Literal["ok", "blocked", "challenge", "thin", "rate_limited"]

CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "checking your browser",
    "verify you are human",
    "captcha",
    "access denied",
    "unusual traffic",
)
BLOCK_MARKERS = ("request blocked", "temporarily blocked", "automated requests")
JS_MARKERS = ('id="root"', 'id="__next"', "<app-root", "<div id='root'")


def classify_response(status: int, headers: dict[str, str], html: str) -> Classification:
    lowered = html.lower()
    normalized_headers = {key.lower(): value.lower() for key, value in headers.items()}
    if status == 429 or (status == 503 and "retry-after" in normalized_headers):
        return "rate_limited"
    if normalized_headers.get("cf-mitigated") == "challenge" or any(
        marker in lowered for marker in CHALLENGE_MARKERS
    ):
        return "challenge"
    if status in (401, 403) or any(marker in lowered for marker in BLOCK_MARKERS):
        return "blocked"
    visible = " ".join(__import__("re").sub(r"<[^>]+>", " ", html).split())
    if (
        status == 200
        and len(visible) < 200
        and (lowered.count("<script") >= 3 or any(marker in lowered for marker in JS_MARKERS))
    ):
        return "thin"
    return "ok"
