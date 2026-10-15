"""OpenAI-compatible JSON chat client with local rate and daily-use limits."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_attempt
from tenacity.wait import wait_base

from leadforge.config import Settings
from leadforge.db import connect, init_schema

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMNotConfigured(RuntimeError):
    """Raised when the base URL, API key, or model is missing."""


class LLMOutputError(RuntimeError):
    """Raised when the provider response cannot be validated after one repair."""


class _RetryableRequest(Exception):
    def __init__(self, message: str, delay_seconds: float) -> None:
        super().__init__(message)
        self.delay_seconds = delay_seconds


class _ProviderWait(wait_base):
    def __call__(self, retry_state) -> float:
        error = retry_state.outcome.exception() if retry_state.outcome else None
        return error.delay_seconds if isinstance(error, _RetryableRequest) else 0.0


class TokenBucket:
    def __init__(
        self,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.rate = max(1, requests_per_minute)
        self.capacity = float(self.rate)
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.updated_at = clock()
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self.lock:
            while True:
                now = self.clock()
                refill = (now - self.updated_at) * self.rate / 60
                self.tokens = min(self.capacity, self.tokens + refill)
                self.updated_at = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await self.sleep((1 - self.tokens) * 60 / self.rate)


class LLMClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings or Settings()
        self.sleep = sleep
        self.utcnow = utcnow
        self.bucket = TokenBucket(
            self.settings.llm_requests_per_minute, clock=monotonic, sleep=sleep
        )

    def _configuration(self) -> tuple[str, str, str]:
        base = self.settings.llm_base_url
        key = self.settings.llm_api_key
        model = self.settings.llm_model
        if not base or not key or not model:
            raise LLMNotConfigured(
                "Set LEADFORGE_LLM_BASE_URL, LEADFORGE_LLM_API_KEY, and "
                "LEADFORGE_LLM_MODEL to use the LLM."
            )
        return base.rstrip("/"), key.get_secret_value(), model

    async def chat_json(
        self,
        system: str,
        user: str,
        schema: type[T],
        *,
        temperature: float = 0.0,
        prompt_version: str = "",
    ) -> T:
        base_url, api_key, model = self._configuration()
        sys_prompt = (
            system
            + "\nReturn only a valid JSON object matching this JSON schema:\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user}]
        start = time.monotonic()
        log.info(
            "LLM request prompt_version=%s system_chars=%d user_chars=%d",
            prompt_version or "unspecified",
            len(sys_prompt),
            len(user),
        )
        content = await self._request_json(base_url, api_key, model, messages, temperature)
        try:
            return schema.model_validate_json(_strip_fence(content))
        except (ValidationError, ValueError, TypeError) as first_error:
            repair_messages = [
                *messages,
                {"role": "assistant", "content": content},
                {
                    "role": "user",
                    "content": (
                        "Repair the previous response so it validates against the required "
                        f"schema. Validation error: {first_error}. Return only JSON."
                    ),
                },
            ]
            content = await self._request_json(
                base_url, api_key, model, repair_messages, temperature
            )
            try:
                result = schema.model_validate_json(_strip_fence(content))
            except (ValidationError, ValueError, TypeError) as repair_error:
                raise LLMOutputError(
                    f"LLM response remained invalid after one repair: {repair_error}"
                ) from repair_error
        log.debug(
            "LLM response prompt_version=%s response_chars=%d latency_seconds=%.3f",
            prompt_version or "unspecified",
            len(content),
            time.monotonic() - start,
        )
        return result

    async def _request_json(
        self,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
    ) -> str:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        body = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        endpoint = f"{base_url}/chat/completions"
        format_supported = True

        async def send_one_attempt(attempt_number: int) -> str:
            nonlocal format_supported
            for _ in range(2):
                await self.bucket.acquire()
                self._increment_daily_count()
                request_body = (
                    body
                    if format_supported
                    else {k: v for k, v in body.items() if k != "response_format"}
                )
                try:
                    async with httpx.AsyncClient(
                        timeout=self.settings.llm_timeout_seconds
                    ) as client:
                        response = await client.post(endpoint, headers=headers, json=request_body)
                except httpx.HTTPError as exc:
                    raise _RetryableRequest(
                        f"LLM transport failed: {exc}", 0.5 * (2 ** (attempt_number - 1))
                    ) from exc
                unsupported_mode = _unsupported_json_mode(response.text)
                if response.status_code == 400 and format_supported and unsupported_mode:
                    format_supported = False
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    raise _RetryableRequest(
                        f"LLM provider returned HTTP {response.status_code}",
                        _retry_delay(response, attempt_number - 1),
                    )
                if response.is_error:
                    raise LLMOutputError(f"LLM provider returned HTTP {response.status_code}")
                try:
                    payload = response.json()
                    content = payload["choices"][0]["message"]["content"]
                except (ValueError, KeyError, IndexError, TypeError) as exc:
                    raise LLMOutputError(
                        "LLM provider response is missing message content"
                    ) from exc
                if not isinstance(content, str):
                    raise LLMOutputError("LLM provider returned non-text message content")
                return content
            raise LLMOutputError("LLM provider rejected JSON mode and did not return a response")

        retrying = AsyncRetrying(
            retry=retry_if_exception_type(_RetryableRequest),
            stop=stop_after_attempt(3),
            wait=_ProviderWait(),
            sleep=self.sleep,
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return await send_one_attempt(attempt.retry_state.attempt_number)
        except _RetryableRequest as exc:
            raise LLMOutputError(f"LLM request failed after retries: {exc}") from exc
        raise LLMOutputError("LLM request exhausted retries")

    def _increment_daily_count(self) -> None:
        day = self.utcnow().astimezone(UTC).date().isoformat()
        conn = connect(self.settings.db_path)
        try:
            init_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT count FROM llm_usage WHERE day=?", (day,)).fetchone()
            used = int(row["count"]) if row else 0
            if used >= self.settings.llm_daily_cap:
                conn.rollback()
                raise LLMOutputError(
                    f"LLM daily request cap reached ({self.settings.llm_daily_cap})"
                )
            conn.execute(
                """INSERT INTO llm_usage(day,count) VALUES(?,1)
                   ON CONFLICT(day) DO UPDATE SET count=count+1""",
                (day,),
            )
            conn.commit()
        finally:
            conn.close()


def _strip_fence(content: str) -> str:
    content = content.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else content


def _unsupported_json_mode(message: str) -> bool:
    lower = message.lower()
    return "response_format" in lower or "json_object" in lower or "json mode" in lower


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("Retry-After")
    if value:
        try:
            return max(0.0, min(60.0, float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value).astimezone(UTC)
                return max(0.0, min(60.0, (retry_at - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                pass
    return 0.5 * (2**attempt)


async def chat_json[T: BaseModel](
    system: str,
    user: str,
    schema: type[T],
    *,
    temperature: float = 0.0,
    settings: Settings | None = None,
    prompt_version: str = "",
) -> T:
    global _DEFAULT_CLIENT
    if settings is None:
        if _DEFAULT_CLIENT is None:
            _DEFAULT_CLIENT = LLMClient()
        client = _DEFAULT_CLIENT
    else:
        client = LLMClient(settings)
    return await client.chat_json(
        system, user, schema, temperature=temperature, prompt_version=prompt_version
    )


_DEFAULT_CLIENT: LLMClient | None = None
