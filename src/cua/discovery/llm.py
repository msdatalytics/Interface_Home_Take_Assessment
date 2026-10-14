"""Provider-neutral LLM interface for the discovery agent.

The agent is *stateless per turn*: every call carries the full, compact task state (goal,
contract, action history, current screen). No growing chat transcript means bounded
context, reproducible prompts, and no stale screen states for the model to confuse.
Every turn is forced to be exactly one tool call (``tool_choice=any/required``).
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class LLMReply:
    tool_call: ToolCall | None
    text: str | None
    model: str
    usage: dict[str, int] = field(default_factory=dict)


class LLMError(RuntimeError):
    pass


class LLMClient(Protocol):
    provider: str
    model: str

    async def decide(
        self, system: str, user: str, tools: Sequence[ToolSpec], image_png: bytes | None = None
    ) -> LLMReply: ...


class AnthropicClient:
    """Claude via the Messages API.

    The system prompt and tool definitions are identical on every turn of a run, so they are
    marked as a prompt-cache prefix; only the per-turn state is billed at the full input rate.
    """

    provider = "anthropic"

    def __init__(self, model: str, *, max_tokens: int = 1024, client: Any = None) -> None:
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(max_retries=4, timeout=90.0)
        self.model = model
        self.max_tokens = max_tokens
        self._client = client

    async def decide(
        self, system: str, user: str, tools: Sequence[ToolSpec], image_png: bytes | None = None
    ) -> LLMReply:
        import anthropic

        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if image_png is not None:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.b64encode(image_png).decode(),
                    },
                }
            )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools],
            "tool_choice": {"type": "any", "disable_parallel_tool_use": True},
            "messages": [{"role": "user", "content": content}],
        }
        try:
            response = await self._client.messages.create(**kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic rejected the API key - check ANTHROPIC_API_KEY in .env") from exc
        except anthropic.PermissionDeniedError as exc:
            raise LLMError(f"this Anthropic key may not use model '{self.model}' ({exc.message})") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(f"unknown Anthropic model '{self.model}' - set CUA_LLM_MODEL or --model") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(f"Anthropic rate limit / credit exhausted after retries ({exc.message})") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"cannot reach the Anthropic API ({exc})") from exc
        except anthropic.APIStatusError as exc:
            if "credit balance" in str(exc):
                raise LLMError("the Anthropic account has no API credits (console.anthropic.com > Billing)") from exc
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

        call = None
        texts: list[str] = []
        for block in response.content:
            if block.type == "tool_use" and call is None:
                call = ToolCall(block.name, dict(block.input))
            elif block.type == "text":
                texts.append(block.text)
        if call is None and response.stop_reason == "max_tokens":
            raise LLMError(f"model reply was cut off at max_tokens={self.max_tokens} before a tool call")
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
            if value := getattr(response.usage, key, None):
                usage[key] = value
        return LLMReply(call, "\n".join(texts) or None, response.model, usage)


class OpenAIClient:
    provider = "openai"

    def __init__(self, model: str) -> None:
        from openai import AsyncOpenAI

        self.model = model
        self._client = AsyncOpenAI()
        self._supports_temperature = True

    async def decide(
        self, system: str, user: str, tools: Sequence[ToolSpec], image_png: bytes | None = None
    ) -> LLMReply:
        from openai import BadRequestError

        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if image_png is not None:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + base64.b64encode(image_png).decode()},
                }
            )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ],
            "tool_choice": "required",
            "parallel_tool_calls": False,
        }
        if self._supports_temperature:
            kwargs["temperature"] = 0
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except BadRequestError as exc:  # reasoning models reject temperature
            if "temperature" not in str(exc) or not self._supports_temperature:
                raise
            self._supports_temperature = False
            kwargs.pop("temperature")
            response = await self._client.chat.completions.create(**kwargs)
        message = response.choices[0].message
        call = None
        if message.tool_calls:
            fn = message.tool_calls[0].function
            try:
                call = ToolCall(fn.name, json.loads(fn.arguments or "{}"))
            except json.JSONDecodeError as exc:
                raise LLMError(f"model returned invalid tool arguments: {exc}") from exc
        usage = {}
        if response.usage:
            usage = {"input_tokens": response.usage.prompt_tokens, "output_tokens": response.usage.completion_tokens}
        return LLMReply(call, message.content, response.model, usage)


class OllamaClient:
    """A local model served by Ollama (native ``/api/chat``: tools, context size, sampling).

    Ollama has no ``tool_choice``, so a reply that writes the call as JSON text instead of a
    structured tool call is parsed as one; anything else counts as "no tool call" and the agent
    re-prompts (and escalates if the model keeps failing).
    """

    provider = "ollama"

    def __init__(self, model: str, *, host: str | None = None, num_ctx: int = 16384, client: Any = None) -> None:
        import httpx

        self.model = model
        self.host = (host or os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.num_ctx = num_ctx
        self._client = client or httpx.AsyncClient(timeout=300.0)
        self._think: bool | None = os.environ.get("CUA_OLLAMA_THINK", "").lower() in ("1", "true", "yes")

    async def decide(
        self, system: str, user: str, tools: Sequence[ToolSpec], image_png: bytes | None = None
    ) -> LLMReply:
        import httpx

        message: dict[str, Any] = {"role": "user", "content": user}
        if image_png is not None:
            message["images"] = [base64.b64encode(image_png).decode()]
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, message],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in tools
            ],
            "stream": False,
            "options": {"temperature": 0, "seed": 7, "num_ctx": self.num_ctx},
        }
        if self._think is not None:
            body["think"] = self._think
        try:
            response = await self._client.post(f"{self.host}/api/chat", json=body)
            if response.status_code == 400 and "think" in response.text and "think" in body:
                self._think = None  # the model has no thinking mode to switch off
                body.pop("think")
                response = await self._client.post(f"{self.host}/api/chat", json=body)
        except httpx.TransportError as exc:
            raise LLMError(f"Ollama is not reachable at {self.host} - start it with `ollama serve` ({exc})") from exc
        if response.status_code == 404:
            raise LLMError(f"model '{self.model}' is not installed - run `ollama pull {self.model}`")
        if response.status_code >= 400:
            raise LLMError(f"Ollama error {response.status_code}: {response.text[:300]}")
        data = response.json()
        reply = data.get("message") or {}
        content = reply.get("content") or None
        text = content or reply.get("thinking") or None
        call = None
        for tool_call in reply.get("tool_calls") or []:
            fn = tool_call.get("function") or {}
            arguments = fn.get("arguments") or {}
            if isinstance(arguments, str):
                arguments = _json_object(arguments) or {}
            call = ToolCall(str(fn.get("name")), arguments)
            break
        if call is None and content:
            call = _tool_call_in_text(content, {t.name for t in tools})
        usage = {"input_tokens": int(data.get("prompt_eval_count", 0)), "output_tokens": int(data.get("eval_count", 0))}
        return LLMReply(call, text, str(data.get("model", self.model)), usage)


def _json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _tool_call_in_text(text: str, names: set[str]) -> ToolCall | None:
    """``{"name": ..., "arguments": {...}}`` written as text (optionally in a code fence or <tool_call>)."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    for candidate in re.findall(r"\{.*\}", text, flags=re.DOTALL):
        value = _json_object(candidate)
        if value and value.get("name") in names:
            arguments = value.get("arguments") or value.get("parameters") or {}
            if isinstance(arguments, str):
                arguments = _json_object(arguments) or {}
            if isinstance(arguments, dict):
                return ToolCall(str(value["name"]), arguments)
    return None


@dataclass(frozen=True)
class ScreenRef:
    """Script placeholder for a ``ref``: the element whose rendering matches ``element``
    (a regex applied after ``[eN] ``), optionally restricted to screen lines matching ``line``."""

    element: str
    line: str | None = None

    def resolve(self, prompt: str) -> str:
        screen = prompt.split("CURRENT SCREEN:", 1)[-1]
        pattern = re.compile(r"\[(e\d+)\] " + self.element)
        for text in screen.splitlines():
            if self.line and not re.search(self.line, text):
                continue
            if match := pattern.search(text):
                return match.group(1)
        raise LLMError(f"scripted ref not on screen: element={self.element!r} line={self.line!r}")


class ScriptedClient:
    """Plays a fixed sequence of tool calls, resolving :class:`ScreenRef` placeholders against
    the screen in each prompt. For tests and offline demos only - never discovery evidence."""

    provider = "scripted"

    def __init__(self, calls: Sequence[ToolCall], model: str = "scripted") -> None:
        self.model = model
        self._calls = list(calls)
        self.prompts: list[str] = []

    async def decide(
        self, system: str, user: str, tools: Sequence[ToolSpec], image_png: bytes | None = None
    ) -> LLMReply:
        self.prompts.append(user)
        if not self._calls:
            return LLMReply(ToolCall("give_up", {"reason": "script exhausted"}), None, self.model)
        call = self._calls.pop(0)
        arguments = {k: v.resolve(user) if isinstance(v, ScreenRef) else v for k, v in call.arguments.items()}
        return LLMReply(ToolCall(call.name, arguments), None, self.model)


DEFAULT_MODELS = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4.1", "ollama": "qwen3:8b"}


def client_from_env(provider: str | None = None, model: str | None = None) -> LLMClient:
    """Pick a provider from arguments, ``CUA_LLM_PROVIDER``, or whichever API key is present."""
    provider = provider or os.environ.get("CUA_LLM_PROVIDER")
    if provider is None:
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        else:
            raise LLMError(
                "no LLM configured: set CUA_LLM_PROVIDER=ollama, ANTHROPIC_API_KEY or OPENAI_API_KEY (see .env.example)"
            )
    if provider not in DEFAULT_MODELS:
        raise LLMError(f"unknown LLM provider '{provider}' (expected one of: {', '.join(DEFAULT_MODELS)})")
    model = model or os.environ.get("CUA_LLM_MODEL") or DEFAULT_MODELS[provider]
    if provider == "ollama":
        return OllamaClient(model)
    key_var = f"{provider.upper()}_API_KEY"
    if not os.environ.get(key_var):
        raise LLMError(f"CUA_LLM_PROVIDER={provider} but {key_var} is not set (add it to .env)")
    if provider == "anthropic":
        return AnthropicClient(model)
    return OpenAIClient(model)
