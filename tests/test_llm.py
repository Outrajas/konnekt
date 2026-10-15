import logging

import pytest
import respx
from httpx import Response
from pydantic import BaseModel, SecretStr
from typer.testing import CliRunner

from leadforge.cli import app
from leadforge.config import Settings
from leadforge.llm.client import LLMClient, LLMNotConfigured, LLMOutputError, TokenBucket


class Ping(BaseModel):
    ok: bool


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "llm_base_url": "https://llm.example/v1",
        "llm_api_key": SecretStr("test-key"),
        "llm_model": "fake-model",
        "llm_timeout_seconds": 1.0,
        "llm_requests_per_minute": 100,
        "llm_daily_cap": 20,
        "db_path": tmp_path / "llm.db",
    }
    values.update(overrides)
    return Settings(**values)


def response(content: str, status_code: int = 200, headers: dict[str, str] | None = None):
    return Response(
        status_code,
        headers=headers,
        json={"choices": [{"message": {"content": content}}]}
        if status_code == 200
        else {"error": {"message": "temporary"}},
    )


@pytest.mark.asyncio
async def test_chat_json_success_sends_openai_compatible_request_without_logging_prompts(
    tmp_path, caplog
) -> None:
    caplog.set_level(logging.INFO)
    route = "https://llm.example/v1/chat/completions"
    with respx.mock(assert_all_called=True) as mock:
        request = mock.post(route).mock(return_value=response('{"ok":true}'))
        result = await LLMClient(make_settings(tmp_path)).chat_json(
            "system prompt", "private page text", Ping, prompt_version="test-v1"
        )
    sent = request.calls[0].request
    assert sent.headers["Authorization"] == "Bearer test-key"
    assert sent.read() and b"response_format" in sent.content
    assert result.ok is True
    assert "test-v1" in caplog.text
    assert "private page text" not in caplog.text


@pytest.mark.asyncio
async def test_chat_json_parses_fenced_json(tmp_path) -> None:
    with respx.mock(assert_all_called=True) as mock:
        mock.post("https://llm.example/v1/chat/completions").mock(
            return_value=response('```json\n{"ok": true}\n```')
        )
        result = await LLMClient(make_settings(tmp_path)).chat_json("sys", "usr", Ping)
    assert result.ok


@pytest.mark.asyncio
async def test_chat_json_repairs_one_invalid_schema_response(tmp_path) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://llm.example/v1/chat/completions")
        route.side_effect = [response('{"wrong":1}'), response('{"ok":true}')]
        result = await LLMClient(make_settings(tmp_path)).chat_json("sys", "usr", Ping)
    assert result.ok
    repair_body = route.calls[1].request.content.decode()
    assert "Validation error" in repair_body


@pytest.mark.asyncio
async def test_chat_json_raises_after_one_invalid_repair(tmp_path) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://llm.example/v1/chat/completions")
        route.side_effect = [response('{"wrong":1}'), response('{"still_wrong":2}')]
        with pytest.raises(LLMOutputError, match="after one repair"):
            await LLMClient(make_settings(tmp_path)).chat_json("sys", "usr", Ping)
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_chat_json_retries_429_and_respects_retry_after(tmp_path) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://llm.example/v1/chat/completions")
        route.side_effect = [
            response("", 429, {"Retry-After": "2"}),
            response('{"ok":true}'),
        ]
        result = await LLMClient(make_settings(tmp_path), sleep=fake_sleep).chat_json(
            "sys", "usr", Ping
        )
    assert result.ok
    assert delays == [2.0]


@pytest.mark.asyncio
async def test_chat_json_retries_provider_5xx_with_backoff(tmp_path) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://llm.example/v1/chat/completions")
        route.side_effect = [Response(503), response('{"ok":true}')]
        result = await LLMClient(make_settings(tmp_path), sleep=fake_sleep).chat_json(
            "sys", "usr", Ping
        )
    assert result.ok
    assert delays == [0.5]


@pytest.mark.asyncio
async def test_chat_json_falls_back_when_json_mode_is_unsupported(tmp_path) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://llm.example/v1/chat/completions")
        route.side_effect = [
            Response(400, json={"error": {"message": "response_format not supported"}}),
            response('{"ok":true}'),
        ]
        result = await LLMClient(make_settings(tmp_path)).chat_json("sys", "usr", Ping)
    assert result.ok
    assert b"response_format" not in route.calls[1].request.content


@pytest.mark.asyncio
async def test_chat_json_enforces_persistent_daily_cap(tmp_path) -> None:
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post("https://llm.example/v1/chat/completions").mock(
            return_value=response('{"ok":true}')
        )
        client = LLMClient(make_settings(tmp_path, llm_daily_cap=1))
        await client.chat_json("sys", "usr", Ping)
        with pytest.raises(LLMOutputError, match="daily request cap"):
            await client.chat_json("sys", "usr", Ping)
        assert route.call_count == 1


@pytest.mark.asyncio
async def test_not_configured_fails_without_http_request(tmp_path) -> None:
    client = LLMClient(make_settings(tmp_path, llm_base_url=None, llm_api_key=None, llm_model=None))
    with pytest.raises(LLMNotConfigured):
        await client.chat_json("sys", "usr", Ping)


def test_llm_ping_handles_missing_configuration(monkeypatch) -> None:
    import leadforge.cli as cli

    async def missing_configuration(*args, **kwargs):
        raise LLMNotConfigured("not configured")

    monkeypatch.setattr(cli, "chat_json", missing_configuration)
    result = CliRunner().invoke(app, ["llm-ping"])
    assert result.exit_code == 0
    assert result.stdout.strip() == "LLM not configured"


@pytest.mark.asyncio
async def test_token_bucket_waits_for_refill() -> None:
    now = [0.0]
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        now[0] += seconds

    bucket = TokenBucket(1, clock=lambda: now[0], sleep=sleep)
    await bucket.acquire()
    await bucket.acquire()
    assert delays == [60.0]
    assert now[0] == 60.0
