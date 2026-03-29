"""Agent entrypoint — run as `python -m runtime`.

Usage:
  # Normal mode (reads task from KB, used by Argo):
  TASK_ID=... MYCROFT_AGENT_TYPE=coder python -m runtime

  # CLI mode (instruction from flag, no KB task needed):
  python -m runtime --agent coder --instruction "add a README to mycroft"

  # Dry run (show prompt only, don't call LLM):
  python -m runtime --agent coder --instruction "add a README" --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from common.config import PlatformConfig
from common.models import AgentManifest, TaskConfig


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("agent")


def main():
    parser = argparse.ArgumentParser(description="Mycroft agent runtime")
    parser.add_argument("--agent", "-a", help="Agent type (e.g. coder)")
    parser.add_argument("--instruction", "-i", help="Task instruction (CLI mode)")
    parser.add_argument("--dry-run", action="store_true", help="Show prompt only, don't call LLM")
    parser.add_argument("--model", "-m", help="Override model from manifest")
    args = parser.parse_args()

    # CLI mode: instruction from flag
    if args.instruction:
        agent_type = args.agent or "coder"
        task_id = str(uuid.uuid4())
    else:
        # Argo mode: from env vars
        task_id = os.environ.get("TASK_ID")
        agent_type = os.environ.get("MYCROFT_AGENT_TYPE") or args.agent
        if not task_id or not agent_type:
            log.error("Either --instruction or TASK_ID + MYCROFT_AGENT_TYPE env vars required")
            sys.exit(1)

    # Find manifest
    repo_root = Path(__file__).resolve().parent.parent
    manifest_path = repo_root / "agents" / agent_type / "manifest.yaml"

    if not manifest_path.exists():
        log.error("Manifest not found: %s", manifest_path)
        sys.exit(1)

    manifest = AgentManifest.from_yaml(manifest_path)
    if args.model:
        manifest.model = args.model
    platform = PlatformConfig()

    task = TaskConfig(id=task_id, agent_type=agent_type)

    if args.instruction:
        task.instruction = args.instruction
        if args.dry_run:
            _dry_run(manifest, task, platform)
            return
        asyncio.run(_run_cli(manifest, task, platform))
    else:
        log.info("Starting agent: type=%s task=%s model=%s max_iter=%d",
                 agent_type, task_id[:8], manifest.model, platform.global_max_iterations)
        asyncio.run(_run_argo(manifest, task, platform))


def _dry_run(manifest: AgentManifest, task: TaskConfig, platform: PlatformConfig):
    """Show the prompt that would be sent to the LLM without calling it."""
    from runtime.context import build_system_prompt, build_user_message
    from runtime.tools.base import load_tools

    tools = load_tools(manifest.tools)
    system_prompt = build_system_prompt(manifest, tools.schemas())
    user_message = build_user_message(task.instruction, [])

    print("=" * 60)
    print("DRY RUN — Agent:", manifest.name)
    print("Model:", manifest.model)
    print("Tools:", [t["function"]["name"] for t in tools.schemas()])
    print("Max iterations:", min(manifest.max_iterations, platform.global_max_iterations))
    print("=" * 60)
    print("\n--- SYSTEM PROMPT ---\n")
    print(system_prompt)
    print("\n--- USER MESSAGE ---\n")
    print(user_message)
    print("\n--- TOOL SCHEMAS ---\n")
    print(json.dumps(tools.schemas(), indent=2))
    print("=" * 60)


async def _discover_llm_key(manifest: AgentManifest, platform: PlatformConfig):
    """Auto-discover LLM API key from llm-manager."""
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


async def _run_cli(manifest: AgentManifest, task: TaskConfig, platform: PlatformConfig):
    """Run agent in CLI mode — instruction from flag, no KB task record needed."""
    from runtime.runner import AgentRunner

    await _discover_llm_key(manifest, platform)

    log.info("CLI mode: type=%s model=%s instruction='%s'",
             manifest.name, manifest.model, task.instruction[:100])

    runner = AgentRunner(manifest, task, platform)
    result = await runner.run()
    print("\n" + "=" * 60)
    print("RESULT:")
    print("=" * 60)
    print(result)


async def _run_argo(manifest: AgentManifest, task: TaskConfig, platform: PlatformConfig):
    """Run agent in Argo mode — reads task from KB."""
    from common.kb import KBClient
    from runtime.runner import AgentRunner

    # Read task instruction from KB inbox
    kb = KBClient(platform.kb_dsn, permissions=None)
    await kb.connect()

    inbox_record = await kb.get(f"/agents/{manifest.name}/inbox/{task.id}")
    if inbox_record:
        task.instruction = inbox_record.content
        if inbox_record.metadata:
            task.config = inbox_record.metadata
            task.repo = inbox_record.metadata.get("repo", "")
    else:
        task_record = await kb.get_task(task.id)
        if task_record and task_record.config:
            task.instruction = task_record.config.get("instruction", "")
            task.repo = task_record.config.get("repo", "")

    await kb.close()

    if not task.instruction:
        log.error("No instruction found for task %s", task.id)
        sys.exit(1)

    log.info("Task instruction: %s", task.instruction[:200])

    await _discover_llm_key(manifest, platform)

    runner = AgentRunner(manifest, task, platform)
    result = await runner.run()
    log.info("Agent completed. Result: %s", result[:500])
