"""Agent entrypoint — run as `python -m runtime`."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from common.config import PlatformConfig
from common.models import AgentManifest, TaskConfig


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("agent")


def main():
    task_id = os.environ.get("TASK_ID")
    agent_type = os.environ.get("MYCROFT_AGENT_TYPE")

    if not task_id or not agent_type:
        log.error("TASK_ID and MYCROFT_AGENT_TYPE env vars are required")
        sys.exit(1)

    # Find manifest relative to repo root
    repo_root = Path(__file__).resolve().parent.parent
    manifest_path = repo_root / "agents" / agent_type / "manifest.yaml"

    if not manifest_path.exists():
        log.error("Manifest not found: %s", manifest_path)
        sys.exit(1)

    manifest = AgentManifest.from_yaml(manifest_path)
    platform = PlatformConfig()

    log.info("Starting agent: type=%s task=%s model=%s max_iter=%d",
             agent_type, task_id[:8], manifest.model, platform.global_max_iterations)

    # Build task config — instruction comes from KB inbox
    task = TaskConfig(
        id=task_id,
        agent_type=agent_type,
    )

    asyncio.run(_run(manifest, task, platform))


async def _run(manifest: AgentManifest, task: TaskConfig, platform: PlatformConfig):
    from common.kb import KBClient
    from runtime.runner import AgentRunner

    # Read task instruction from KB inbox
    kb = KBClient(platform.kb_dsn, permissions=None)  # full access for reading inbox
    await kb.connect()

    inbox_record = await kb.get(f"/agents/{manifest.name}/inbox/{task.id}")
    if inbox_record:
        task.instruction = inbox_record.content
        if inbox_record.metadata:
            task.config = inbox_record.metadata
            task.repo = inbox_record.metadata.get("repo", "")
    else:
        # Fallback: read from agent_tasks config
        task_record = await kb.get_task(task.id)
        if task_record and task_record.config:
            task.instruction = task_record.config.get("instruction", "")
            task.repo = task_record.config.get("repo", "")

    await kb.close()

    if not task.instruction:
        log.error("No instruction found for task %s", task.id)
        sys.exit(1)

    log.info("Task instruction: %s", task.instruction[:200])

    # Auto-discover LLM API key if registration secret is set
    if platform.llm_registration_secret and not platform.llm_manager_api_key:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{platform.llm_manager_url}/api/apps/discover",
                    json={
                        "name": f"mycroft-{manifest.name}",
                        "base_url": "",
                        "registration_secret": platform.llm_registration_secret,
                    },
                )
                resp.raise_for_status()
                platform.llm_manager_api_key = resp.json().get("api_key", "")
                log.info("Discovered LLM API key (key=%s...)", platform.llm_manager_api_key[:8])
        except Exception as e:
            log.error("Failed to discover LLM API key: %s", e)

    # Run the agent
    runner = AgentRunner(manifest, task, platform)
    result = await runner.run()
    log.info("Agent completed. Result: %s", result[:500])
