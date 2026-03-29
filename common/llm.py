"""Thin LLM client — talks to llm-manager's OpenAI-compatible API."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string


@dataclass
class ChatResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    """Async client for llm-manager /v1/chat/completions."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/v1",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=300.0,
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ChatResponse:
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        log.debug("LLM request: model=%s messages=%d tools=%d",
                   payload["model"], len(messages), len(tools or []))

        resp = await self._client.post("/chat/completions", json=payload)
        resp.raise_for_status()
        data = resp.json()

        choice = data["choices"][0]["message"]
        tool_calls = []
        for tc in choice.get("tool_calls") or []:
            tool_calls.append(ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=tc["function"]["arguments"],
            ))

        return ChatResponse(
            content=choice.get("content") or "",
            tool_calls=tool_calls,
            raw=data,
        )

    async def close(self):
        await self._client.aclose()
