"""Pydantic models for the agent platform."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TaskStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class IntentType(str, enum.Enum):
    engineering = "engineering"
    general = "general"
    system = "system"


# ---------------------------------------------------------------------------
# Agent manifest (loaded from agents/{name}/manifest.yaml)
# ---------------------------------------------------------------------------


class AgentPermissions(BaseModel):
    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)


class AgentResources(BaseModel):
    memory: str = "512Mi"
    cpu: str = "1"
    scratch: str = "5Gi"


class TriggerRule(BaseModel):
    event: str
    filter: dict[str, Any] = Field(default_factory=dict)


class AgentManifest(BaseModel):
    name: str
    role: str = ""
    goal: str = ""
    model: str = "claude-sonnet-4-20250514"
    backend: str = "k8s"
    max_concurrent: int = 2
    max_iterations: int = 10
    resources: AgentResources = Field(default_factory=AgentResources)
    tools: list[str] = Field(default_factory=list)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    triggers: list[TriggerRule] = Field(default_factory=list)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AgentManifest:
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)


# ---------------------------------------------------------------------------
# Task config (per invocation)
# ---------------------------------------------------------------------------


class TaskConfig(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    agent_type: str
    instruction: str = ""
    repo: str = ""
    branch: str = ""
    model_override: str | None = None
    system_prompt_override: str | None = None
    timeout_override: int | None = None
    max_iterations_override: int | None = None
    context_injection: list[str] = Field(default_factory=list)
    parent_task_id: str | None = None
    trigger: str = "manual"
    trigger_ref: str = ""
    config: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# KB records
# ---------------------------------------------------------------------------


class MemoryRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    content: str
    scope: str
    categories: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    importance: float = 0.5
    source: str | None = None
    needs_embedding: bool = True
    created_at: datetime | None = None


# ---------------------------------------------------------------------------
# Task record (from agent_tasks table)
# ---------------------------------------------------------------------------


class TaskRecord(BaseModel):
    id: str
    agent_type: str
    status: TaskStatus = TaskStatus.pending
    trigger: str = "manual"
    trigger_ref: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    result: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Intent classification result
# ---------------------------------------------------------------------------


class Intent(BaseModel):
    type: IntentType = IntentType.engineering
    agent_type: str | None = None
    repo: str | None = None
    instruction: str = ""
