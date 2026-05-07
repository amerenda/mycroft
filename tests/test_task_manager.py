"""Tests for coordinator/task_manager.py — task lifecycle with mocked KBClient."""

from __future__ import annotations

from unittest.mock import AsyncMock

from common.models import TaskRecord, TaskStatus
from coordinator.task_manager import TaskManager


def _make_kb() -> AsyncMock:
    return AsyncMock()


# ── create_task ───────────────────────────────────────────────────────────────

async def test_create_task_returns_id():
    kb = _make_kb()
    kb.create_task.return_value = "task-id-abc"
    mgr = TaskManager(kb)
    tid = await mgr.create_task("coder", "Fix the bug")
    assert tid == "task-id-abc"


async def test_create_task_calls_kb_with_correct_agent_and_trigger():
    kb = _make_kb()
    kb.create_task.return_value = "tid"
    mgr = TaskManager(kb)
    await mgr.create_task("researcher", "Research topic", trigger="api", trigger_ref="req-1")
    kb.create_task.assert_awaited_once()
    kwargs = kb.create_task.call_args.kwargs
    assert kwargs["agent_type"] == "researcher"
    assert kwargs["trigger"] == "api"
    assert kwargs["trigger_ref"] == "req-1"


async def test_create_task_writes_instruction_to_agent_inbox():
    kb = _make_kb()
    kb.create_task.return_value = "tid-123"
    mgr = TaskManager(kb)
    await mgr.create_task("coder", "Implement feature X")
    kb.write.assert_awaited_once()
    write_kwargs = kb.write.call_args.kwargs
    assert "tid-123" in write_kwargs["scope"]
    assert "coder" in write_kwargs["scope"]
    assert write_kwargs["content"] == "Implement feature X"


async def test_create_task_inbox_scope_under_agents_path():
    kb = _make_kb()
    kb.create_task.return_value = "my-task"
    mgr = TaskManager(kb)
    await mgr.create_task("writer", "Draft report")
    scope = kb.write.call_args.kwargs["scope"]
    assert scope.startswith("/agents/writer/inbox/")


async def test_create_task_merges_repo_into_config():
    kb = _make_kb()
    kb.create_task.return_value = "tid"
    mgr = TaskManager(kb)
    await mgr.create_task("coder", "Fix bug", repo="ecdysis")
    config = kb.create_task.call_args.kwargs["config"]
    assert config["repo"] == "ecdysis"


async def test_create_task_merges_extra_config():
    kb = _make_kb()
    kb.create_task.return_value = "tid"
    mgr = TaskManager(kb)
    await mgr.create_task("coder", "Fix bug", config={"model_override": "llama3.1:8b"})
    config = kb.create_task.call_args.kwargs["config"]
    assert config["model_override"] == "llama3.1:8b"
    assert config["instruction"] == "Fix bug"


# ── can_launch ────────────────────────────────────────────────────────────────

async def test_can_launch_when_under_limit():
    kb = _make_kb()
    kb.count_running_tasks.return_value = 1
    assert await TaskManager(kb).can_launch("coder", max_concurrent=2) is True


async def test_cannot_launch_at_limit():
    kb = _make_kb()
    kb.count_running_tasks.return_value = 2
    assert await TaskManager(kb).can_launch("coder", max_concurrent=2) is False


async def test_cannot_launch_over_limit():
    kb = _make_kb()
    kb.count_running_tasks.return_value = 5
    assert await TaskManager(kb).can_launch("coder", max_concurrent=3) is False


async def test_can_launch_zero_running():
    kb = _make_kb()
    kb.count_running_tasks.return_value = 0
    assert await TaskManager(kb).can_launch("coder", max_concurrent=1) is True


# ── get_task / list_tasks ─────────────────────────────────────────────────────

async def test_get_task_delegates_to_kb():
    kb = _make_kb()
    expected = TaskRecord(id="abc", agent_type="coder", status=TaskStatus.completed)
    kb.get_task.return_value = expected
    result = await TaskManager(kb).get_task("abc")
    assert result == expected
    kb.get_task.assert_awaited_once_with("abc")


async def test_get_task_returns_none_when_not_found():
    kb = _make_kb()
    kb.get_task.return_value = None
    assert await TaskManager(kb).get_task("missing") is None


async def test_list_tasks_delegates_with_all_filters():
    kb = _make_kb()
    records = [
        TaskRecord(id="1", agent_type="coder", status=TaskStatus.running),
        TaskRecord(id="2", agent_type="coder", status=TaskStatus.pending),
    ]
    kb.list_tasks.return_value = records
    result = await TaskManager(kb).list_tasks(
        agent_type="coder", status=TaskStatus.running, limit=10
    )
    assert result == records
    kb.list_tasks.assert_awaited_once_with(
        agent_type="coder", status=TaskStatus.running, limit=10
    )
