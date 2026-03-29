"""Trigger router — matches events to agent types."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from common.models import AgentManifest

log = logging.getLogger(__name__)


class TriggerRouter:
    """Matches incoming events to agent types based on manifests."""

    def __init__(self):
        self.manifests: dict[str, AgentManifest] = {}

    def load_manifests(self, agents_dir: str | Path) -> None:
        """Load all agent manifests from the agents directory."""
        agents_path = Path(agents_dir)
        for manifest_file in agents_path.glob("*/manifest.yaml"):
            manifest = AgentManifest.from_yaml(manifest_file)
            self.manifests[manifest.name] = manifest
            log.info("Loaded manifest: %s (triggers=%d)", manifest.name, len(manifest.triggers))

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
