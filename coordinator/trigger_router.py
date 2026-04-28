"""Trigger router — matches events to agent types."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from common.models import AgentManifest

log = logging.getLogger(__name__)


def _extract_prompt_text(text: str) -> str:
    """Return plain prompt text from DB prompts field.

    New saves from the UI are stored as plain text directly.
    Old records seeded before this change may still be in Python prompts.py
    format (SYSTEM_SUPPLEMENT = \"\"\"...\"\"\"). Handle both for backward compat.
    """
    if "SYSTEM_SUPPLEMENT" not in text:
        return text.strip()
    import re
    m = re.search(r'SYSTEM_SUPPLEMENT\s*=\s*"""\s*([\s\S]*?)\s*"""', text)
    return m.group(1).strip() if m else text.strip()


class TriggerRouter:
    """Matches incoming events to agent types based on manifests."""

    def __init__(self):
        self.manifests: dict[str, AgentManifest] = {}
        self.prompts: dict[str, str] = {}  # agent_name → prompts text (DB-sourced agents)

    def load_manifests(self, agents_dir: str | Path) -> None:
        """Load all agent manifests from the agents directory."""
        agents_path = Path(agents_dir)
        for manifest_file in agents_path.glob("*/manifest.yaml"):
            manifest = AgentManifest.from_yaml(manifest_file)
            self.manifests[manifest.name] = manifest
            log.info("Loaded manifest: %s (triggers=%d)", manifest.name, len(manifest.triggers))

    def register(self, name: str, manifest_yaml: str, prompts_text: str = "") -> AgentManifest | None:
        """Register or update an agent from raw YAML (used for DB-sourced agents)."""
        try:
            data = yaml.safe_load(manifest_yaml)
            if not data or not isinstance(data, dict):
                return None
            data.setdefault("name", name)
            manifest = AgentManifest(**data)
            self.manifests[manifest.name] = manifest
            if prompts_text:
                self.prompts[manifest.name] = _extract_prompt_text(prompts_text)
            log.info("Registered agent from DB: %s", manifest.name)
            return manifest
        except Exception as e:
            log.warning("Failed to register agent '%s': %s", name, e)
            return None

    def unregister(self, name: str) -> None:
        """Remove an agent from the router."""
        self.manifests.pop(name, None)
        self.prompts.pop(name, None)

    def get_prompts(self, name: str) -> str:
        """Return stored prompts text for a DB-sourced agent (empty string if none)."""
        return self.prompts.get(name, "")

    def route(self, event_type: str, payload: dict[str, Any]) -> list[str]:
        """Find agent types that should handle this event. Returns list of agent type names."""
        matches = []
        for name, manifest in self.manifests.items():
            for trigger in manifest.triggers:
                if trigger.event != event_type:
                    continue
                if self._matches_filter(payload, trigger.filter):
                    matches.append(name)
                    break
        return matches

    def _matches_filter(self, payload: dict[str, Any], filter_spec: dict[str, Any]) -> bool:
        """Check if payload matches the trigger filter."""
        if not filter_spec:
            return True
        for key, value in filter_spec.items():
            if key not in payload:
                return False
            if isinstance(value, list):
                if payload[key] not in value:
                    return False
            elif payload[key] != value:
                return False
        return True

    def get_manifest(self, agent_type: str) -> AgentManifest | None:
        return self.manifests.get(agent_type)
