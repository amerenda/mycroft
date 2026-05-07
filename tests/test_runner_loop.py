"""Tests for runtime/runner.py — agent loop, text tool call parsing, and force-finish fallback."""

from __future__ import annotations

import json
import pytest
from unittest.mock import AsyncMock, patch

from common.config import PlatformConfig
from common.llm import ChatResponse, ToolCall
from common.models import AgentManifest, TaskConfig, TaskStatus
from runtime.runner import AgentRunner
from runtime.tools.base import Finish, SubmitReport, ToolRegistry


# ── helpers ───────────────────────────────────────────────────────────────────

def _manifest(**kw) -> AgentManifest:
    return AgentManifest(name="tester", role="Tester", goal="Test things", **kw)


def _task(**kw) -> TaskConfig:
    return TaskConfig(agent_type="tester", instruction="test task", **kw)


def _platform(**kw) -> PlatformConfig:
    return PlatformConfig(
        kb_dsn="postgresql://test",
        llm_manager_url="http://llm",
        llm_manager_api_key="key",
        global_max_iterations=30,
        **kw,
    )


def _make_runner(manifest=None, task=None, platform=None, tools=None) -> AgentRunner:
    """Create AgentRunner with mocked LLM and KB, injecting a real ToolRegistry."""
    m = manifest or _manifest()
    t = task or _task()
    p = platform or _platform()
    with patch("runtime.runner.LLMClient"), patch("runtime.runner.KBClient"):
        runner = AgentRunner(m, t, p)
    runner.tools = tools or ToolRegistry([Finish()])
    runner.llm = AsyncMock()
    runner.kb = AsyncMock()
    runner.kb.get.return_value = None    # fresh start (no prior conversation)
    runner.kb.recall.return_value = []   # no KB context
    return runner


def _finish_response(content: str = "done") -> ChatResponse:
    return ChatResponse(
        content="",
        tool_calls=[ToolCall(id="tc1", name="finish", arguments=json.dumps({"content": content}))],
    )


class _NonFinishTool:
    name = "search"
    description = "search"
    parameters: dict = {"type": "object", "properties": {}}

    async def execute(self, args: dict) -> str:
        return "search result"


def _search_response() -> ChatResponse:
    return ChatResponse(
        content="",
        tool_calls=[ToolCall(id="tc1", name="search", arguments="{}")],
    )


# ── _parse_text_tool_call ─────────────────────────────────────────────────────

class TestParseTextToolCall:
    def test_json_code_block_finish(self):
        runner = _make_runner()
        text = '```json\n{"name": "finish", "parameters": {"content": "my result"}}\n```'
        result = runner._parse_text_tool_call(text)
        assert result is not None
        name, args = result
        assert name == "finish"
        assert json.loads(args)["content"] == "my result"

    def test_plain_json_finish(self):
        runner = _make_runner()
        text = '{"name": "finish", "parameters": {"content": "output"}}'
        result = runner._parse_text_tool_call(text)
        assert result is not None
        assert result[0] == "finish"

    def test_json_with_function_key(self):
        runner = _make_runner()
        text = '{"function": "finish", "arguments": {"content": "result"}}'
        result = runner._parse_text_tool_call(text)
        assert result is not None
        assert result[0] == "finish"

    def test_unknown_tool_returns_none(self):
        runner = _make_runner()
        assert runner._parse_text_tool_call('{"name": "unknown_tool", "parameters": {}}') is None

    def test_non_json_returns_none(self):
        runner = _make_runner()
        assert runner._parse_text_tool_call("just text, no JSON at all") is None

    def test_empty_string_returns_none(self):
        runner = _make_runner()
        assert runner._parse_text_tool_call("") is None

    def test_terminal_tool_preferred_over_non_terminal(self):
        class NoopTool:
            name = "noop"
            description = "noop"
            parameters: dict = {"type": "object", "properties": {}}
            async def execute(self, args: dict) -> str: return ""

        runner = _make_runner(tools=ToolRegistry([Finish(), NoopTool()]))
        # noop appears first, finish appears second — finish should be preferred
        text = (
            "```json\n{\"name\": \"noop\", \"parameters\": {}}\n```\n"
            "```json\n{\"name\": \"finish\", \"parameters\": {\"content\": \"f\"}}\n```"
        )
        result = runner._parse_text_tool_call(text)
        assert result is not None
        assert result[0] == "finish"

    def test_code_block_without_json_marker(self):
        runner = _make_runner()
        text = '```\n{"name": "finish", "parameters": {"content": "ok"}}\n```'
        result = runner._parse_text_tool_call(text)
        assert result is not None
        assert result[0] == "finish"


# ── _force_finish_fallback_text ───────────────────────────────────────────────

class TestForceFinishFallbackText:
    def test_returns_last_assistant_text(self):
        runner = _make_runner()
        runner.messages = [
            {"role": "assistant", "content": "Earlier note"},
            {"role": "tool", "tool_call_id": "1", "content": "tool result"},
            {"role": "assistant", "content": "Final answer"},
        ]
        assert runner._force_finish_fallback_text() == "Final answer"

    def test_falls_back_to_tool_output_when_no_assistant_text(self):
        runner = _make_runner()
        runner.messages = [
            {"role": "user", "content": "query"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "tool result text"},
        ]
        result = runner._force_finish_fallback_text()
        assert "tool result text" in result

    def test_empty_messages_returns_empty(self):
        runner = _make_runner()
        runner.messages = []
        assert runner._force_finish_fallback_text() == ""

    def test_skips_whitespace_only_assistant_messages(self):
        runner = _make_runner()
        runner.messages = [
            {"role": "assistant", "content": "   "},
            {"role": "tool", "tool_call_id": "1", "content": "actual result"},
        ]
        result = runner._force_finish_fallback_text()
        assert "actual result" in result

    def test_truncates_very_long_tool_output(self):
        runner = _make_runner()
        runner.messages = [
            {"role": "tool", "tool_call_id": "1", "content": "x" * 20_000},
        ]
        result = runner._force_finish_fallback_text()
        assert "truncated" in result
        assert len(result) < 20_000


# ── AgentRunner.run() ─────────────────────────────────────────────────────────

async def test_run_finish_tool_returns_result():
    runner = _make_runner()
    runner.llm.chat.return_value = _finish_response("The answer is 42")
    result = await runner.run()
    assert result == "The answer is 42"


async def test_run_marks_task_running_then_completed():
    runner = _make_runner()
    runner.llm.chat.return_value = _finish_response("done")
    await runner.run()
    statuses = [c.kwargs.get("status") for c in runner.kb.update_task.call_args_list]
    assert TaskStatus.running in statuses
    assert TaskStatus.completed in statuses


async def test_run_writes_result_content_to_kb():
    runner = _make_runner()
    runner.llm.chat.return_value = _finish_response("my output")
    await runner.run()
    written = [c.kwargs.get("content", "") for c in runner.kb.write.call_args_list]
    assert any("my output" in w for w in written)


async def test_run_closes_kb_and_llm_after_success():
    runner = _make_runner()
    runner.llm.chat.return_value = _finish_response("done")
    await runner.run()
    runner.kb.close.assert_awaited_once()
    runner.llm.close.assert_awaited_once()


async def test_run_marks_failed_and_reraises_on_exception():
    runner = _make_runner()
    runner.llm.chat.side_effect = RuntimeError("LLM exploded")
    with pytest.raises(RuntimeError, match="LLM exploded"):
        await runner.run()
    statuses = [c.kwargs.get("status") for c in runner.kb.update_task.call_args_list]
    assert TaskStatus.failed in statuses


async def test_run_closes_kb_and_llm_after_exception():
    runner = _make_runner()
    runner.llm.chat.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        await runner.run()
    runner.kb.close.assert_awaited_once()
    runner.llm.close.assert_awaited_once()


async def test_run_text_response_without_require_tool_exit_returns_immediately():
    runner = _make_runner()
    runner.llm.chat.return_value = ChatResponse(content="Plain text answer", tool_calls=[])
    result = await runner.run()
    assert result == "Plain text answer"


async def test_run_max_iterations_force_finishes_via_tool_loop():
    runner = _make_runner(
        manifest=_manifest(max_iterations=2),
        tools=ToolRegistry([Finish(), _NonFinishTool()]),
    )
    # Never call finish — always call search
    runner.llm.chat.return_value = _search_response()
    result = await runner.run()
    # After 2 iterations (max_iterations=2), force-finish fallback assembles tool output
    assert "search result" in result


async def test_run_empty_responses_nudged_then_finished():
    runner = _make_runner(manifest=_manifest(max_iterations=5))
    empty = ChatResponse(content="", tool_calls=[])
    finish = _finish_response("done after nudge")
    runner.llm.chat.side_effect = [empty, empty, finish]
    result = await runner.run()
    assert result == "done after nudge"


async def test_run_three_consecutive_empty_responses_force_finishes():
    runner = _make_runner(manifest=_manifest(max_iterations=10))
    runner.llm.chat.return_value = ChatResponse(content="", tool_calls=[])
    # 3 empty responses → break → force_finish_fallback → empty string → run() returns ""
    result = await runner.run()
    # The result may be empty or contain a message — just check run() doesn't hang
    assert isinstance(result, str)


async def test_run_text_tool_call_from_json_in_content():
    runner = _make_runner()
    # LLM emits finish as JSON text instead of real API tool call
    text_finish = '```json\n{"name": "finish", "parameters": {"content": "text-tool result"}}\n```'
    runner.llm.chat.return_value = ChatResponse(content=text_finish, tool_calls=[])
    result = await runner.run()
    assert result == "text-tool result"
