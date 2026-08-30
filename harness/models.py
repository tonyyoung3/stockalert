from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelTurn:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class Model(Protocol):
    def complete(self, messages: list[dict], tools: list[dict]) -> ModelTurn:
        """Return the next assistant turn. `tools` is OpenAI function-schema format."""


class ScriptedModel:
    """Deterministic model for tests and local replay. Each complete() pops one turn."""

    def __init__(self, turns: list[ModelTurn]):
        self._turns = list(turns)

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelTurn:
        if not self._turns:
            raise RuntimeError("ScriptedModel has no remaining turns")
        return self._turns.pop(0)


class OpenAICompatModel:
    """Thin Chat Completions client (OpenAI or any compatible /v1 endpoint)."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
    ):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> OpenAICompatModel:
        api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("HARNESS_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY (or HARNESS_API_KEY) is not set. "
                "Use `python -m harness --tool ...` to call tools without a model."
            )
        return cls(
            api_key=api_key,
            model=os.environ.get("HARNESS_MODEL", "gpt-4o-mini"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )

    def complete(self, messages: list[dict], tools: list[dict]) -> ModelTurn:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Model HTTP {exc.code}: {detail}") from exc

        message = body["choices"][0]["message"]
        raw_calls = message.get("tool_calls") or []
        tool_calls = []
        for raw in raw_calls:
            fn = raw.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            tool_calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{len(tool_calls)}"),
                    name=str(fn.get("name") or ""),
                    arguments=args,
                )
            )
        return ModelTurn(content=message.get("content"), tool_calls=tool_calls)
