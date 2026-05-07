"""Tests for runtime/tools/ — ToolRegistry, load_tools, shell, and web noise filtering."""

from __future__ import annotations

import json
import pytest

from runtime.tools.base import Finish, SubmitReport, ToolRegistry, load_tools
from runtime.tools.web import _is_noise


# ── Finish / SubmitReport ─────────────────────────────────────────────────────

async def test_finish_returns_content_arg():
    assert await Finish().execute({"content": "my output"}) == "my output"


async def test_finish_empty_content():
    assert await Finish().execute({}) == ""


async def test_submit_report_returns_content_arg():
    assert await SubmitReport().execute({"content": "the report"}) == "the report"


async def test_submit_report_has_correct_name():
    assert SubmitReport().name == "submit_report"


# ── ToolRegistry ──────────────────────────────────────────────────────────────

class TestToolRegistry:
    def test_schemas_returns_openai_compatible_format(self):
        registry = ToolRegistry([Finish()])
        schemas = registry.schemas()
        assert len(schemas) == 1
        s = schemas[0]
        assert s["type"] == "function"
        assert "function" in s
        assert s["function"]["name"] == "finish"
        assert "description" in s["function"]
        assert "parameters" in s["function"]

    def test_schemas_includes_all_registered_tools(self):
        registry = ToolRegistry([Finish(), SubmitReport()])
        names = {s["function"]["name"] for s in registry.schemas()}
        assert names == {"finish", "submit_report"}

    async def test_execute_dispatches_to_correct_tool(self):
        registry = ToolRegistry([Finish()])
        result = await registry.execute("finish", '{"content": "hello"}')
        assert result == "hello"

    async def test_execute_unknown_tool_returns_error_string(self):
        registry = ToolRegistry([Finish()])
        result = await registry.execute("nonexistent", "{}")
        assert "unknown tool" in result.lower() or "nonexistent" in result

    async def test_execute_invalid_json_returns_error_string(self):
        registry = ToolRegistry([Finish()])
        result = await registry.execute("finish", "not valid json{")
        assert "error" in result.lower() or "invalid" in result.lower()

    async def test_execute_empty_arguments_string(self):
        registry = ToolRegistry([Finish()])
        result = await registry.execute("finish", "")
        assert isinstance(result, str)

    async def test_execute_tool_exception_returns_error_string(self):
        class BrokenTool:
            name = "breaker"
            description = "breaks"
            parameters: dict = {"type": "object", "properties": {}}
            async def execute(self, args: dict) -> str:
                raise ValueError("intentional failure")

        registry = ToolRegistry([BrokenTool()])
        result = await registry.execute("breaker", "{}")
        assert "error" in result.lower() or "breaker" in result.lower()

    def test_empty_registry_schemas_is_empty_list(self):
        assert ToolRegistry([]).schemas() == []


# ── load_tools group expansion ────────────────────────────────────────────────

class TestLoadTools:
    def test_empty_names_returns_empty_registry(self):
        registry = load_tools([])
        assert registry.schemas() == []

    def test_at_prefix_expands_group(self):
        registry = load_tools(["@shell"])
        names = {s["function"]["name"] for s in registry.schemas()}
        assert "run_command" in names

    def test_bare_group_name_expands(self):
        registry = load_tools(["shell"])
        names = {s["function"]["name"] for s in registry.schemas()}
        assert "run_command" in names

    def test_individual_tool_name_loaded(self):
        registry = load_tools(["submit_report"])
        names = {s["function"]["name"] for s in registry.schemas()}
        assert "submit_report" in names

    def test_web_group_loads_web_tools(self):
        registry = load_tools(["web"])
        names = {s["function"]["name"] for s in registry.schemas()}
        assert "web_search" in names
        assert "web_read" in names
        assert "wiki_read" in names

    def test_scratch_scope_adds_finish_and_scratch_tools(self, tmp_path):
        registry = load_tools(
            [],
            kb_dsn="postgresql://test",
            scratch_scope="/tasks/t1/scratch",
        )
        names = {s["function"]["name"] for s in registry.schemas()}
        assert "finish" in names
        assert "scratch_read" in names
        assert "scratch_write" in names

    def test_no_scratch_scope_no_finish_injected(self):
        registry = load_tools([])
        names = {s["function"]["name"] for s in registry.schemas()}
        assert "finish" not in names

    def test_extra_groups_override_builtin(self):
        custom_groups = {"mygroup": ["submit_report"]}
        registry = load_tools(["mygroup"], extra_groups=custom_groups)
        names = {s["function"]["name"] for s in registry.schemas()}
        assert "submit_report" in names


# ── WebSearch noise filtering ─────────────────────────────────────────────────

class TestIsNoise:
    def test_linkedin_is_noise(self):
        assert _is_noise({"url": "https://www.linkedin.com/in/johndoe", "score": 1.0})

    def test_facebook_is_noise(self):
        assert _is_noise({"url": "https://facebook.com/page", "score": 1.0})

    def test_normal_url_not_noise(self):
        assert not _is_noise({"url": "https://example.com/article", "score": 0.5})

    def test_low_score_is_noise(self):
        assert _is_noise({"url": "https://legit.com/page", "score": 0.05})

    def test_score_at_threshold_not_noise(self):
        # default threshold is 0.1 — at exactly 0.1 it should pass
        assert not _is_noise({"url": "https://example.com/", "score": 0.1})

    def test_subdomain_of_noise_domain_is_noise(self):
        assert _is_noise({"url": "https://help.linkedin.com/something", "score": 1.0})

    def test_empty_url_not_noise(self):
        # empty url won't match any noise domain — score determines it
        assert not _is_noise({"url": "", "score": 0.9})

    def test_missing_score_defaults_to_not_noise(self):
        # score defaults to 1.0 in the implementation when absent
        result = _is_noise({"url": "https://example.com/"})
        assert not result


# ── RunCommand tool ───────────────────────────────────────────────────────────

class TestRunCommand:
    async def test_runs_simple_command(self, tmp_path):
        from runtime.tools.shell import RunCommand
        tool = RunCommand(str(tmp_path))
        result = await tool.execute({"command": "echo hello"})
        assert "hello" in result

    async def test_nonzero_exit_includes_exit_code(self, tmp_path):
        from runtime.tools.shell import RunCommand
        tool = RunCommand(str(tmp_path))
        result = await tool.execute({"command": "exit 1"})
        assert "1" in result

    async def test_stderr_included_in_output(self, tmp_path):
        from runtime.tools.shell import RunCommand
        tool = RunCommand(str(tmp_path))
        result = await tool.execute({"command": "echo errtext >&2"})
        assert "errtext" in result

    async def test_relative_cwd_resolved_against_workspace(self, tmp_path):
        from runtime.tools.shell import RunCommand
        subdir = tmp_path / "sub"
        subdir.mkdir()
        tool = RunCommand(str(tmp_path))
        result = await tool.execute({"command": "pwd", "cwd": "sub"})
        assert "sub" in result

    async def test_no_output_returns_sentinel(self, tmp_path):
        from runtime.tools.shell import RunCommand
        tool = RunCommand(str(tmp_path))
        result = await tool.execute({"command": "true"})
        assert result == "(no output)"

    async def test_has_correct_name_and_description(self, tmp_path):
        from runtime.tools.shell import RunCommand
        tool = RunCommand(str(tmp_path))
        assert tool.name == "run_command"
        assert "command" in tool.description.lower() or "shell" in tool.description.lower()
