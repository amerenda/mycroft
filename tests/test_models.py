"""Tests for common/models.py — Pydantic model validation."""

import json
import tempfile
from pathlib import Path

import yaml

from common.models import (
    AgentManifest,
    AgentPermissions,
    Intent,
    IntentType,
    MemoryRecord,
    TaskConfig,
    TaskRecord,
    TaskStatus,
)


class TestAgentManifest:
    def test_defaults(self):
        m = AgentManifest(name="test")
        assert m.name == "test"
        assert m.model == "claude-sonnet-4-20250514"
        assert m.max_iterations == 10
        assert m.max_concurrent == 2
        assert m.tools == []
        assert m.permissions.read == []

    def test_full_manifest(self):
        m = AgentManifest(
            name="coder",
            role="Senior software engineer",
            goal="Write correct code",
            model="qwen2.5:72b",
            tools=["git", "github", "shell"],
            permissions=AgentPermissions(
                read=["/agents/coder/inbox", "/wiki"],
                write=["/agents/coder/results"],
            ),
        )
        assert m.name == "coder"
        assert "git" in m.tools
        assert "/wiki" in m.permissions.read

    def test_from_yaml(self):
        manifest_data = {
            "name": "coder",
            "role": "Engineer",
            "goal": "Write code",
            "model": "claude-sonnet-4-20250514",
            "tools": ["git", "shell"],
            "permissions": {
                "read": ["/agents/coder/inbox"],
                "write": ["/agents/coder/results"],
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(manifest_data, f)
            f.flush()
            m = AgentManifest.from_yaml(f.name)

        assert m.name == "coder"
        assert m.tools == ["git", "shell"]


class TestTaskConfig:
    def test_defaults(self):
        t = TaskConfig(agent_type="coder")
        assert t.agent_type == "coder"
        assert t.id  # UUID generated
        assert t.trigger == "manual"
        assert t.instruction == ""

    def test_full_config(self):
        t = TaskConfig(
            agent_type="coder",
            instruction="Fix the login bug",
            repo="ecdysis",
            trigger="telegram",
            trigger_ref="msg-123",
        )
        assert t.instruction == "Fix the login bug"
        assert t.repo == "ecdysis"


class TestMemoryRecord:
    def test_defaults(self):
        r = MemoryRecord(content="hello", scope="/test")
        assert r.content == "hello"
        assert r.importance == 0.5
        assert r.needs_embedding is True
        assert r.categories == []

    def test_serialization(self):
        r = MemoryRecord(
            content="test",
            scope="/wiki/auth",
            categories=["auth", "ecdysis"],
            metadata={"pr_url": "https://github.com/..."},
        )
        data = r.model_dump()
        r2 = MemoryRecord(**data)
        assert r2.categories == ["auth", "ecdysis"]
        assert r2.metadata["pr_url"] == "https://github.com/..."


class TestTaskRecord:
    def test_status_enum(self):
        t = TaskRecord(id="abc", agent_type="coder", status=TaskStatus.running)
        assert t.status == TaskStatus.running
        assert t.status.value == "running"


class TestIntent:
    def test_engineering_intent(self):
        i = Intent(
            type=IntentType.engineering,
            agent_type="coder",
            repo="ecdysis",
            instruction="Fix the login bug",
        )
        assert i.type == IntentType.engineering
        assert i.agent_type == "coder"

    def test_system_intent(self):
        i = Intent(type=IntentType.system, instruction="What's the status?")
        assert i.agent_type is None
