"""LLM-based intent classification for incoming messages."""

from __future__ import annotations

import json
import logging

from common.llm import LLMClient
from common.models import Intent, IntentType

log = logging.getLogger(__name__)

CLASSIFY_PROMPT = """You are an intent classifier for an AI agent platform. Classify the user's message into one of these categories:

1. "engineering" — A software engineering task: fix a bug, add a feature, refactor code, write tests, update docs. These go to the coder agent.
2. "research" — A research request: investigate a topic, compare options, find best practices, evaluate tools, answer a technical question. These go to the researcher agent.
3. "system" — A question about agent status, task progress, or system state. The coordinator answers directly.
4. "general" — Everything else: calendar, reminders, email, general knowledge questions. These go to the personal assistant.

For engineering tasks, also identify:
- agent_type: "coder"
- repo: the repository name if mentioned (e.g., "ecdysis", "llm-manager", "mycroft")

For research tasks:
- agent_type: "researcher"
- effort: "light" for quick questions, "regular" for normal research (default), "deep" for thorough investigation
  Hints: "quick research" / "briefly" = light. "research" / "look into" = regular. "deep research" / "thorough" / "comprehensive" = deep.

Respond with a JSON object:
{"type": "engineering"|"research"|"system"|"general", "agent_type": "coder"|"researcher"|null, "repo": "repo-name"|null, "effort": "light"|"regular"|"deep"|null, "instruction": "the full task description"}

Only respond with the JSON object, nothing else."""


async def classify(text: str, llm: LLMClient) -> Intent:
    """Classify a message into an intent using the LLM."""
    messages = [
        {"role": "system", "content": CLASSIFY_PROMPT},
        {"role": "user", "content": text},
    ]

    try:
        response = await llm.chat(messages)
        data = json.loads(response.content)
        return Intent(
            type=IntentType(data.get("type", "engineering")),
            agent_type=data.get("agent_type"),
            repo=data.get("repo"),
            effort=data.get("effort"),
            instruction=data.get("instruction", text),
        )
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Intent classification failed, defaulting to engineering: %s", e)
        return Intent(
            type=IntentType.engineering,
            agent_type="coder",
            instruction=text,
        )
