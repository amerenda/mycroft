"""Tests for common/llm.py — result parsing, job context, status details, and chat flow."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from common.llm import ChatResponse, LLMClient, ToolCall


def _client() -> LLMClient:
    """LLMClient with its httpx client replaced by an AsyncMock."""
    c = LLMClient("http://llm-manager", "api-key", "qwen3:14b")
    c._client = AsyncMock()
    return c


# ── _parse_result ─────────────────────────────────────────────────────────────

class TestParseResult:
    def test_text_response(self):
        result = {
            "choices": [{"message": {"role": "assistant", "content": "hello world"}}],
            "usage": {},
        }
        resp = _client()._parse_result(result)
        assert resp.content == "hello world"
        assert resp.tool_calls == []

    def test_null_content_becomes_empty_string(self):
        result = {
            "choices": [{"message": {"role": "assistant", "content": None}}],
            "usage": {},
        }
        resp = _client()._parse_result(result)
        assert resp.content == ""

    def test_tool_calls_parsed(self):
        result = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "tc-1",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query": "QUIC"}',
                            },
                        }
                    ],
                }
            }],
            "usage": {},
        }
        resp = _client()._parse_result(result)
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc.id == "tc-1"
        assert tc.name == "web_search"
        assert json.loads(tc.arguments)["query"] == "QUIC"

    def test_multiple_tool_calls(self):
        result = {
            "choices": [{
                "message": {
                    "content": "",
                    "tool_calls": [
                        {"id": "1", "function": {"name": "a", "arguments": "{}"}},
                        {"id": "2", "function": {"name": "b", "arguments": "{}"}},
                    ],
                }
            }],
            "usage": {},
        }
        resp = _client()._parse_result(result)
        assert len(resp.tool_calls) == 2
        assert {tc.name for tc in resp.tool_calls} == {"a", "b"}

    def test_no_tool_calls_key_means_empty_list(self):
        result = {
            "choices": [{"message": {"content": "text", "tool_calls": None}}],
            "usage": {},
        }
        resp = _client()._parse_result(result)
        assert resp.tool_calls == []


# ── _job_context ──────────────────────────────────────────────────────────────

class TestJobContext:
    def test_metadata_with_known_keys_formatted(self):
        job = {"metadata": {"source": "ecdysis", "runner_id": 5, "host": "murderbot"}}
        ctx = LLMClient._job_context(job)
        assert "source=ecdysis" in ctx
        assert "runner_id=5" in ctx
        assert "host=murderbot" in ctx

    def test_no_metadata_returns_empty(self):
        assert LLMClient._job_context({}) == ""

    def test_empty_metadata_returns_empty(self):
        assert LLMClient._job_context({"metadata": {}}) == ""

    def test_none_values_skipped(self):
        job = {"metadata": {"source": None, "runner_id": 5}}
        ctx = LLMClient._job_context(job)
        assert "source" not in ctx
        assert "runner_id=5" in ctx

    def test_non_dict_metadata_returns_empty(self):
        assert LLMClient._job_context({"metadata": "not-a-dict"}) == ""


# ── _status_detail ────────────────────────────────────────────────────────────

class TestStatusDetail:
    def test_queued_mentions_wait(self):
        detail = LLMClient._status_detail("queued", "model")
        assert "waiting" in detail.lower() or "queue" in detail.lower()

    def test_loading_model_mentions_model(self):
        detail = LLMClient._status_detail("loading_model", "qwen3:14b")
        assert "qwen3:14b" in detail

    def test_running_mentions_inference(self):
        detail = LLMClient._status_detail("running", "model")
        assert "inference" in detail.lower() or "started" in detail.lower()

    def test_unknown_status_returns_empty_string(self):
        assert LLMClient._status_detail("made_up_status", "model") == ""


# ── chat — submit + poll ──────────────────────────────────────────────────────

async def test_chat_submits_and_returns_on_completed():
    client = _client()

    submit_resp = MagicMock()
    submit_resp.status_code = 200
    submit_resp.raise_for_status.return_value = None
    submit_resp.json.return_value = {"job_id": "j123", "position": 0}
    client._client.post.return_value = submit_resp

    poll_resp = MagicMock()
    poll_resp.status_code = 200
    poll_resp.raise_for_status.return_value = None
    poll_resp.json.return_value = {
        "status": "completed",
        "result": {
            "choices": [{"message": {"content": "the answer", "tool_calls": None}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    }
    client._client.get.return_value = poll_resp

    response = await client.chat([{"role": "user", "content": "hello"}])
    assert response.content == "the answer"
    assert response.prompt_tokens == 10
    assert response.completion_tokens == 5


async def test_chat_raises_on_failed_job():
    client = _client()

    submit_resp = MagicMock()
    submit_resp.status_code = 200
    submit_resp.raise_for_status.return_value = None
    submit_resp.json.return_value = {"job_id": "j999", "position": 0}
    client._client.post.return_value = submit_resp

    poll_resp = MagicMock()
    poll_resp.status_code = 200
    poll_resp.raise_for_status.return_value = None
    poll_resp.json.return_value = {"status": "failed", "error": "OOM killed"}
    client._client.get.return_value = poll_resp

    with pytest.raises(RuntimeError, match="OOM killed"):
        await client.chat([{"role": "user", "content": "hi"}])


async def test_chat_raises_on_rejected_job():
    client = _client()

    submit_resp = MagicMock()
    submit_resp.status_code = 422
    submit_resp.json.return_value = {"message": "model not available"}
    client._client.post.return_value = submit_resp

    with pytest.raises(RuntimeError, match="Queue rejected"):
        await client.chat([{"role": "user", "content": "hi"}])


async def test_chat_raises_on_server_error():
    client = _client()

    submit_resp = MagicMock()
    submit_resp.status_code = 500
    submit_resp.text = "internal error"
    submit_resp.reason_phrase = "Internal Server Error"
    client._client.post.return_value = submit_resp

    with pytest.raises(RuntimeError, match="queue submit failed"):
        await client.chat([{"role": "user", "content": "hi"}])


async def test_chat_emits_metrics_callback():
    client = _client()
    events: list[tuple] = []
    client.set_metrics_callback(lambda e, l, v=1.0: events.append((e, l, v)))

    submit_resp = MagicMock()
    submit_resp.status_code = 200
    submit_resp.raise_for_status.return_value = None
    submit_resp.json.return_value = {"job_id": "jm", "position": 0}
    client._client.post.return_value = submit_resp

    poll_resp = MagicMock()
    poll_resp.status_code = 200
    poll_resp.raise_for_status.return_value = None
    poll_resp.json.return_value = {
        "status": "completed",
        "result": {
            "choices": [{"message": {"content": "ok", "tool_calls": None}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3},
        },
    }
    client._client.get.return_value = poll_resp

    await client.chat([{"role": "user", "content": "test"}])
    emitted_names = {e[0] for e in events}
    assert "llm_call_total_seconds" in emitted_names


async def test_close_calls_aclose():
    client = _client()
    await client.close()
    client._client.aclose.assert_awaited_once()
