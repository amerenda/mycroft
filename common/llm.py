"""Thin LLM client — submits jobs to llm-manager's queue API."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

JOB_TIMEOUT = 3600       # 1 hour — total timeout for inference under GPU contention
QUEUE_WAIT_TIMEOUT = 300 # 5 min — if still queued after this, no runner is available
JOB_POLL_INTERVAL = 2    # seconds between status polls


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
    queue_wait_seconds: float = 0.0
    inference_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


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
        self._metrics_callback: Optional[callable] = None
        self._current_job_id: str | None = None

    def set_metrics_callback(self, callback: callable) -> None:
        """Set a callback for metrics: callback(event_name, labels_dict, value)."""
        self._metrics_callback = callback

    def _emit(self, event: str, labels: dict, value: float = 1.0) -> None:
        if self._metrics_callback:
            try:
                self._metrics_callback(event, labels, value)
            except Exception:
                pass

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
    ) -> ChatResponse:
        effective_model = model or self.model
        payload: dict[str, Any] = {
            "model": effective_model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if temperature is not None:
            payload["temperature"] = temperature

        log.debug("LLM request: model=%s messages=%d tools=%d",
                   effective_model, len(messages), len(tools or []))

        t_submit = time.monotonic()

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
        position = job.get("position", 0)
        warning = job.get("warning")

        log.info("Queue job %s submitted (model=%s, position=%d%s)",
                 job_id, effective_model, position,
                 f", warning: {warning}" if warning else "")

        if position > 0:
            self._emit("llm_queue_position", {"model": effective_model}, position)

        # Poll for completion
        result, wait_secs, inference_secs = await self._wait_for_job(job_id, effective_model)

        t_total = time.monotonic() - t_submit
        self._emit("llm_call_total_seconds", {"model": effective_model}, t_total)
        self._emit("llm_queue_wait_seconds", {"model": effective_model}, wait_secs)

        response = self._parse_result(result)
        response.queue_wait_seconds = wait_secs
        response.inference_seconds = inference_secs

        usage = result.get("usage", {})
        response.prompt_tokens = usage.get("prompt_tokens", 0)
        response.completion_tokens = usage.get("completion_tokens", 0)
        self._emit("llm_tokens", {"model": effective_model, "type": "prompt"}, response.prompt_tokens)
        self._emit("llm_tokens", {"model": effective_model, "type": "completion"}, response.completion_tokens)

        return response

    async def cancel_current_job(self) -> None:
        """Cancel the in-flight llm-manager job, if any. Safe to call with no active job."""
        job_id = self._current_job_id
        if not job_id:
            return
        try:
            await self._client.delete(f"/api/queue/jobs/{job_id}")
            log.info("Cancelled pending LLM job %s on shutdown", job_id)
        except Exception as e:
            log.warning("Failed to cancel LLM job %s on shutdown: %s", job_id, e)

    async def _wait_for_job(self, job_id: str, model: str) -> tuple[dict, float, float]:
        """Poll job status until terminal state or timeout.
        Returns (result_dict, queue_wait_seconds, inference_seconds).
        """
        self._current_job_id = job_id
        elapsed = 0
        last_status = None
        t_start = time.monotonic()
        t_running = None
        t_first_queued = None

        try:
            while elapsed < JOB_TIMEOUT:
                resp = await self._client.get(f"/api/queue/jobs/{job_id}")
                resp.raise_for_status()
                job = resp.json()
                status = job["status"]

                if status != last_status:
                    if status == "running" and t_running is None:
                        t_running = time.monotonic()
                    if status == "queued" and t_first_queued is None:
                        t_first_queued = time.monotonic()
                    log.info("Queue job %s: %s%s", job_id, status,
                             self._status_detail(status, model))
                    self._emit("llm_job_status", {"model": model, "status": status})
                    last_status = status

                # Fast-fail if no runner has picked up the job within QUEUE_WAIT_TIMEOUT.
                # Staying in 'queued' this long means no runner is available for the model.
                if status == "queued" and t_first_queued is not None:
                    queued_secs = time.monotonic() - t_first_queued
                    if queued_secs > QUEUE_WAIT_TIMEOUT:
                        try:
                            await self._client.delete(f"/api/queue/jobs/{job_id}")
                        except Exception:
                            pass
                        raise RuntimeError(
                            f"LLM job {job_id} stuck in queue for {queued_secs:.0f}s — "
                            f"no runner available for model '{model}'"
                        )

                if status == "completed":
                    t_done = time.monotonic()
                    queue_wait = (t_running or t_done) - t_start
                    inference = t_done - (t_running or t_start)
                    return job["result"], queue_wait, inference
                elif status == "failed":
                    raise RuntimeError(f"LLM job {job_id} failed: {job.get('error') or 'unknown'}")
                elif status == "cancelled":
                    raise RuntimeError(f"LLM job {job_id} was cancelled")

                await asyncio.sleep(JOB_POLL_INTERVAL)
                elapsed += JOB_POLL_INTERVAL

            log.warning("Queue job %s timed out after %ds, cancelling", job_id, JOB_TIMEOUT)
            try:
                await self._client.delete(f"/api/queue/jobs/{job_id}")
            except Exception:
                pass
            raise RuntimeError(f"LLM job {job_id} timed out after {JOB_TIMEOUT}s")
        finally:
            self._current_job_id = None

    @staticmethod
    def _status_detail(status: str, model: str) -> str:
        details = {
            "queued": " (waiting for model time)",
            "loading_model": f" (loading {model} into VRAM)",
            "waiting_for_eviction": f" (evicting other model to make room for {model})",
            "running": " (inference started)",
        }
        return details.get(status, "")

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
