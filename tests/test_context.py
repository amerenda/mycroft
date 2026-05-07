"""Tests for runtime/context.py — prompt building and tool round counting."""

from __future__ import annotations

from common.models import AgentManifest, MemoryRecord
from runtime.context import (
    DEFAULT_EMPTY_RESPONSE_NUDGE,
    build_system_prompt,
    build_user_message,
    count_tool_rounds,
    default_iteration_limit_message,
)


def _manifest(**kw) -> AgentManifest:
    defaults = {"name": "unit", "role": "Tester", "goal": "Verify things"}
    defaults.update(kw)
    return AgentManifest(**defaults)


def _record(scope: str, content: str) -> MemoryRecord:
    return MemoryRecord(content=content, scope=scope)


# ── build_system_prompt ───────────────────────────────────────────────────────

class TestBuildSystemPrompt:
    def test_empty_template_returns_empty_string(self):
        assert build_system_prompt(_manifest(), [], template="") == ""

    def test_whitespace_only_template_returns_empty(self):
        assert build_system_prompt(_manifest(), [], template="   ") == ""

    def test_none_template_returns_empty(self):
        assert build_system_prompt(_manifest(), [], template=None) == ""

    def test_template_formatted_with_role_and_goal(self):
        m = _manifest(role="Engineer", goal="Write code")
        tmpl = "Role: {role}\nGoal: {goal}\nMax: {max_iterations}\nTools: {tool_list}\nExit: {exit_tool}"
        result = build_system_prompt(m, [], template=tmpl)
        assert "Role: Engineer" in result
        assert "Goal: Write code" in result

    def test_tool_list_included_in_output(self):
        schema = [{"type": "function", "function": {"name": "web_search", "description": "Search the web"}}]
        tmpl = "{role} {goal} {max_iterations} {exit_tool}\n{tool_list}"
        result = build_system_prompt(_manifest(), schema, template=tmpl)
        assert "web_search: Search the web" in result

    def test_finish_detected_as_exit_tool(self):
        schema = [
            {"type": "function", "function": {"name": "finish", "description": "done"}},
            {"type": "function", "function": {"name": "web_search", "description": "search"}},
        ]
        tmpl = "{exit_tool} {role} {goal} {max_iterations} {tool_list}"
        result = build_system_prompt(_manifest(), schema, template=tmpl)
        assert "finish" in result

    def test_submit_report_detected_as_exit_tool(self):
        schema = [{"type": "function", "function": {"name": "submit_report", "description": "submit"}}]
        tmpl = "{exit_tool} {role} {goal} {max_iterations} {tool_list}"
        result = build_system_prompt(_manifest(), schema, template=tmpl)
        assert "submit_report" in result

    def test_thinking_true_prepends_think_prefix(self):
        m = _manifest(thinking=True)
        result = build_system_prompt(m, [], template="body")
        assert result.startswith("/think\n\n")
        assert "body" in result

    def test_thinking_false_prepends_no_think_prefix(self):
        m = _manifest(thinking=False)
        result = build_system_prompt(m, [], template="body")
        assert result.startswith("/no_think\n\n")

    def test_thinking_none_no_prefix(self):
        m = _manifest(thinking=None)
        result = build_system_prompt(m, [], template="body")
        assert not result.startswith("/think")
        assert not result.startswith("/no_think")

    def test_max_iterations_override_wins_over_manifest(self):
        m = _manifest(max_iterations=10)
        tmpl = "max={max_iterations} {role} {goal} {exit_tool} {tool_list}"
        result = build_system_prompt(m, [], template=tmpl, max_iterations=25)
        assert "max=25" in result
        assert "max=10" not in result

    def test_max_iterations_defaults_to_manifest_value(self):
        m = _manifest(max_iterations=7)
        tmpl = "max={max_iterations} {role} {goal} {exit_tool} {tool_list}"
        result = build_system_prompt(m, [], template=tmpl)
        assert "max=7" in result


# ── build_user_message ────────────────────────────────────────────────────────

class TestBuildUserMessage:
    def test_no_context_returns_instruction_only(self):
        assert build_user_message("fix the bug", []) == "fix the bug"

    def test_context_records_appended(self):
        records = [
            _record("/wiki/auth", "Auth uses JWT tokens"),
            _record("/wiki/db", "DB is PostgreSQL"),
        ]
        msg = build_user_message("fix auth", records)
        assert "fix auth" in msg
        assert "Relevant context from knowledge base:" in msg
        assert "/wiki/auth" in msg
        assert "Auth uses JWT tokens" in msg
        assert "/wiki/db" in msg

    def test_context_content_capped_at_300_chars(self):
        long_content = "x" * 500
        records = [_record("/scope/a", long_content)]
        msg = build_user_message("task", records)
        assert "x" * 300 in msg
        assert "x" * 301 not in msg

    def test_empty_instruction_with_context(self):
        records = [_record("/a", "note")]
        msg = build_user_message("", records)
        assert "/a" in msg
        assert "note" in msg


# ── count_tool_rounds ─────────────────────────────────────────────────────────

class TestCountToolRounds:
    def test_empty_messages_is_zero(self):
        assert count_tool_rounds([]) == 0

    def test_no_tool_calls_is_zero(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        assert count_tool_rounds(msgs) == 0

    def test_one_round(self):
        msgs = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "result"},
        ]
        assert count_tool_rounds(msgs) == 1

    def test_three_rounds(self):
        msgs = [
            {"role": "assistant", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "r1"},
            {"role": "assistant", "tool_calls": [{"id": "2"}]},
            {"role": "tool", "tool_call_id": "2", "content": "r2"},
            {"role": "assistant", "tool_calls": [{"id": "3"}]},
            {"role": "tool", "tool_call_id": "3", "content": "r3"},
        ]
        assert count_tool_rounds(msgs) == 3

    def test_tool_message_without_prior_assistant_tool_calls(self):
        # tool message alone doesn't count
        msgs = [{"role": "tool", "tool_call_id": "1", "content": "r"}]
        assert count_tool_rounds(msgs) == 0


# ── helpers ───────────────────────────────────────────────────────────────────

def test_default_iteration_limit_message_contains_count_and_preview():
    msg = default_iteration_limit_message(15, "fix the login bug")
    assert "15" in msg
    assert "fix the login bug" in msg


def test_default_empty_response_nudge_is_nonempty_string():
    assert isinstance(DEFAULT_EMPTY_RESPONSE_NUDGE, str)
    assert len(DEFAULT_EMPTY_RESPONSE_NUDGE) > 10
