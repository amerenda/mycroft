"""Tests for runtime.task_prompts resolution (DB/UI vs code defaults)."""

from __future__ import annotations

from common.models import AgentManifest, MemoryRecord, TaskConfig
from runtime.context import build_system_prompt, build_user_message
from runtime.task_prompts import (
    resolve_initial_user_message_sync,
    resolve_system_prompt,
    resolve_tool_names,
)


def _manifest() -> AgentManifest:
    return AgentManifest(name="unit", role="Tester", goal="Verify prompts", tools=["web"])


def _task(**config) -> TaskConfig:
    return TaskConfig(id="t1", agent_type="unit", instruction="Hello task", config=dict(config))


def test_resolve_system_prompt_default_matches_build():
    manifest = _manifest()
    task = _task()
    schemas: list = []
    assert resolve_system_prompt(task, manifest, schemas) == build_system_prompt(
        manifest, schemas, effort=None
    )


def test_resolve_system_prompt_config_wins():
    manifest = _manifest()
    task = _task(system_prompt="CUSTOM SYSTEM")
    assert resolve_system_prompt(task, manifest, []) == "CUSTOM SYSTEM"


def test_resolve_system_prompt_empty_string_falls_back():
    manifest = _manifest()
    task = _task(system_prompt="   ")
    assert resolve_system_prompt(task, manifest, []) == build_system_prompt(manifest, [], effort=None)


def test_resolve_system_prompt_task_field_override():
    manifest = _manifest()
    task = _task()
    task.system_prompt_override = "FROM FIELD"
    assert resolve_system_prompt(task, manifest, []) == "FROM FIELD"


def test_resolve_system_prompt_legacy_key():
    manifest = _manifest()
    task = _task(system_prompt_override="LEGACY")
    assert resolve_system_prompt(task, manifest, []) == "LEGACY"


def test_resolve_initial_user_message_override():
    manifest = _manifest()
    task = _task(user_message="Plain user")
    assert resolve_initial_user_message_sync(task, []) == "Plain user"


def test_resolve_initial_user_message_kb_recall_off():
    manifest = _manifest()
    task = _task(kb_recall=False)
    rec = MemoryRecord(content="ctx", scope="/x")
    assert resolve_initial_user_message_sync(task, [rec]) == "Hello task"


def test_resolve_initial_user_message_with_recall():
    manifest = _manifest()
    task = _task()
    rec = MemoryRecord(content="ctx body", scope="/scope/a")
    assert resolve_initial_user_message_sync(task, [rec]) == build_user_message("Hello task", [rec])


def test_resolve_tool_names_from_config_tools():
    manifest = _manifest()
    task = _task(tools=["read_file", "list_files"])
    assert resolve_tool_names(task, manifest) == ["read_file", "list_files"]


def test_resolve_tool_names_tools_override_alias():
    manifest = _manifest()
    task = _task(tools_override=["run_command"])
    assert resolve_tool_names(task, manifest) == ["run_command"]


def test_resolve_tool_names_manifest_default():
    manifest = _manifest()
    task = _task()
    assert resolve_tool_names(task, manifest) == ["web"]
