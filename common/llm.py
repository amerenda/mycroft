"""Thin LLM client — submits jobs to llm-manager's queue API."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Per-call timeout: how long to wait for a single LLM inference job
JOB_TIMEOUT = 600  # 10 minutes — covers model swap + inference for large models
JOB_POLL_INTERVAL = 2  # seconds between status polls


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
    """Async client for llm-manager queue API."""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
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

        # Submit to queue
        resp = await self._client.post("/api/queue/submit", json=payload)
        if resp.status_code == 422:
            detail = resp.json()
            raise RuntimeError(f"Queue rejected job: {detail.get('message', detail)}")
        if resp.status_code == 429:
            raise RuntimeError(f"Queue rate limited: {resp.text}")
        resp.raise_for_status()

        job = resp.json()
        job_id = job["job_id"]
        log.info("Queue job %s submitted (model=%s)", job_id, payload["model"])

        # Poll for completion
        result = await self._wait_for_job(job_id)
        return self._parse_result(result)

    async def _wait_for_job(self, job_id: str) -> dict[str, Any]:
        """Poll job status until terminal state or timeout."""
        elapsed = 0
        last_status = None

        while elapsed < JOB_TIMEOUT:
            resp = await self._client.get(f"/api/queue/jobs/{job_id}")
            resp.raise_for_status()
            job = resp.json()
            status = job["status"]

            if status != last_status:
                log.info("Queue job %s: %s", job_id, status)
                last_status = status

            if status == "completed":
                return job["result"]
            elif status == "failed":
                raise RuntimeError(f"LLM job {job_id} failed: {job.get('error', 'unknown')}")
            elif status == "cancelled":
                raise RuntimeError(f"LLM job {job_id} was cancelled")

            await asyncio.sleep(JOB_POLL_INTERVAL)
            elapsed += JOB_POLL_INTERVAL

        # Timeout — cancel the stuck job
        log.warning("Queue job %s timed out after %ds, cancelling", job_id, JOB_TIMEOUT)
        try:
            await self._client.delete(f"/api/queue/jobs/{job_id}")
        except Exception:
            pass
        raise RuntimeError(f"LLM job {job_id} timed out after {JOB_TIMEOUT}s")

    def _parse_result(self, result: dict[str, Any]) -> ChatResponse:
        """Parse OpenAI-compatible result from queue job."""
        choice = result["choices"][0]["message"]
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
            raw=result,
        )

    async def close(self):
        await self._client.aclose()
