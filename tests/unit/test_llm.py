"""LLM provider adapters: request shape, reply parsing and error translation (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import httpx2
import pytest
from dotenv import dotenv_values

from cua.discovery.llm import AnthropicClient, LLMError, OllamaClient, ToolSpec, client_from_env

TOOLS = [ToolSpec("click", "Click an element", {"type": "object", "properties": {"ref": {"type": "string"}}})]


class FakeMessages:
    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def message(*blocks: Any, stop_reason: str = "tool_use", extra: dict[str, int] | None = None) -> Any:
    return SimpleNamespace(
        content=list(blocks),
        stop_reason=stop_reason,
        model="claude-test",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5, **(extra or {})),
    )


def tool_use(name: str, **arguments: Any) -> Any:
    return SimpleNamespace(type="tool_use", name=name, input=arguments)


def status_error(cls: type[anthropic.APIStatusError], code: int, text: str) -> anthropic.APIStatusError:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(text, response=httpx2.Response(code, request=request), body=None)


def client(*replies: Any) -> tuple[AnthropicClient, FakeMessages]:
    messages = FakeMessages(*replies)
    return AnthropicClient("claude-test", client=SimpleNamespace(messages=messages)), messages


async def test_request_forces_exactly_one_tool_call_and_caches_the_static_prefix() -> None:
    llm, messages = client(message(tool_use("click", ref="e3")))
    reply = await llm.decide("SYSTEM", "TURN STATE", TOOLS)

    assert reply.tool_call is not None
    assert (reply.tool_call.name, reply.tool_call.arguments) == ("click", {"ref": "e3"})
    sent = messages.calls[0]
    assert sent["tool_choice"] == {"type": "any", "disable_parallel_tool_use": True}
    assert sent["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert sent["tools"][0]["input_schema"] == TOOLS[0].parameters
    assert sent["messages"] == [{"role": "user", "content": [{"type": "text", "text": "TURN STATE"}]}]


async def test_cache_usage_is_reported() -> None:
    llm, _ = client(message(tool_use("click", ref="e1"), extra={"cache_read_input_tokens": 900}))
    reply = await llm.decide("S", "U", TOOLS)
    assert reply.usage == {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900}


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (status_error(anthropic.AuthenticationError, 401, "invalid x-api-key"), "ANTHROPIC_API_KEY"),
        (status_error(anthropic.NotFoundError, 404, "model: claude-test"), "CUA_LLM_MODEL"),
        (status_error(anthropic.RateLimitError, 429, "rate limited"), "rate limit"),
        (status_error(anthropic.InternalServerError, 529, "overloaded"), "529"),
        (status_error(anthropic.BadRequestError, 400, "messages: bad"), "400"),
        (status_error(anthropic.BadRequestError, 400, "Your credit balance is too low"), "no API credits"),
    ],
)
async def test_api_errors_become_actionable_llm_errors(error: Exception, expected: str) -> None:
    llm, _ = client(error)
    with pytest.raises(LLMError, match=expected):
        await llm.decide("S", "U", TOOLS)


async def test_truncated_reply_is_an_error_not_a_silent_no_op() -> None:
    llm, _ = client(message(SimpleNamespace(type="text", text="Let me think"), stop_reason="max_tokens"))
    with pytest.raises(LLMError, match="max_tokens"):
        await llm.decide("S", "U", TOOLS)


def test_missing_key_fails_before_any_browser_work(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY is not set"):
        client_from_env("anthropic")


def test_provider_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.delenv("CUA_LLM_PROVIDER", raising=False)
    monkeypatch.delenv("CUA_LLM_MODEL", raising=False)
    llm = client_from_env()
    assert (llm.provider, llm.model) == ("anthropic", "claude-sonnet-5-5")
    with pytest.raises(LLMError, match="unknown LLM provider"):
        client_from_env("gemini")


def test_env_example_defaults_to_the_free_local_provider() -> None:
    values = dotenv_values(Path(__file__).parents[2] / ".env.example")
    assert values["CUA_LLM_PROVIDER"] == "ollama"


# ------------------------------------------------------------------------------ ollama (local models)


def ollama(*responses: httpx.Response) -> tuple[OllamaClient, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return queue.pop(0)

    transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OllamaClient("qwen3:8b", host="http://ollama.test", client=transport), sent


def chat(message: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"model": "qwen3:8b", "message": message, "prompt_eval_count": 7, "eval_count": 3})


async def test_ollama_structured_tool_call_and_request_options() -> None:
    llm, sent = ollama(
        chat({"content": "", "tool_calls": [{"function": {"name": "click", "arguments": {"ref": "e4"}}}]})
    )
    reply = await llm.decide("SYSTEM", "TURN", TOOLS)
    assert reply.tool_call is not None
    assert (reply.tool_call.name, reply.tool_call.arguments) == ("click", {"ref": "e4"})
    assert reply.usage == {"input_tokens": 7, "output_tokens": 3}
    body = sent[0]
    assert body["options"]["temperature"] == 0
    assert body["options"]["num_ctx"] >= 8192
    assert body["think"] is False
    assert body["stream"] is False
    assert body["tools"][0]["function"]["name"] == "click"


@pytest.mark.parametrize(
    "text",
    [
        '{"name": "click", "arguments": {"ref": "e4"}}',
        'I will click.\n```json\n{"name": "click", "arguments": "{\\"ref\\": \\"e4\\"}"}\n```',
        '<think>hmm</think><tool_call>{"name": "click", "parameters": {"ref": "e4"}}</tool_call>',
    ],
)
async def test_ollama_tool_call_written_as_text_is_recovered(text: str) -> None:
    llm, _ = ollama(chat({"content": text}))
    reply = await llm.decide("S", "U", TOOLS)
    assert reply.tool_call is not None
    assert (reply.tool_call.name, reply.tool_call.arguments) == ("click", {"ref": "e4"})


async def test_ollama_prose_or_unknown_tool_is_no_call() -> None:
    llm, _ = ollama(chat({"content": "The search button is e4."}), chat({"content": '{"name": "rm", "arguments": {}}'}))
    assert (await llm.decide("S", "U", TOOLS)).tool_call is None
    assert (await llm.decide("S", "U", TOOLS)).tool_call is None


async def test_ollama_model_without_thinking_mode_is_retried_without_the_flag() -> None:
    no_think = httpx.Response(400, json={"error": '"llama3.1" does not support thinking'})
    ok = chat({"tool_calls": [{"function": {"name": "click", "arguments": {"ref": "e1"}}}]})
    llm, sent = ollama(no_think, ok, ok)
    await llm.decide("S", "U", TOOLS)
    await llm.decide("S", "U", TOOLS)
    assert "think" in sent[0]
    assert "think" not in sent[1]
    assert "think" not in sent[2]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(404, json={"error": "model 'qwen3:8b' not found"}), "ollama pull qwen3:8b"),
        (httpx.Response(500, json={"error": "out of memory"}), "Ollama error 500"),
    ],
)
async def test_ollama_errors_are_actionable(response: httpx.Response, expected: str) -> None:
    llm, _ = ollama(response)
    with pytest.raises(LLMError, match=expected):
        await llm.decide("S", "U", TOOLS)


async def test_ollama_not_running() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    llm = OllamaClient("qwen3:8b", host="http://ollama.test", client=client)
    with pytest.raises(LLMError, match="ollama serve"):
        await llm.decide("S", "U", TOOLS)


def test_ollama_needs_no_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CUA_LLM_MODEL", raising=False)
    llm = client_from_env("ollama")
    assert (llm.provider, llm.model) == ("ollama", "qwen3:8b")
