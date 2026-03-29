"""LLM-based intent classification for incoming messages."""

from __future__ import annotations

import json
import logging

from common.llm import LLMClient
from common.models import Intent, IntentType

log = logging.getLogger(__name__)

CLASSIFY_PROMPT = """You are an intent classifier for an AI agent platform. Classify the user's message into one of these categories:

1. "engineering" — A software engineering task: fix a bug, add a feature, refactor code, write tests, update docs. These go to engineering agents.
2. "system" — A question about agent status, task progress, or system state. The coordinator answers directly.
3. "general" — Everything else: calendar, reminders, email, general knowledge questions. These go to the personal assistant.

For engineering tasks, also identify:
- agent_type: always "coder" for now
- repo: the repository name if mentioned (e.g., "ecdysis", "llm-manager", "mycroft")

Respond with a JSON object:
{"type": "engineering"|"system"|"general", "agent_type": "coder"|null, "repo": "repo-name"|null, "instruction": "the full task description"}

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
            instruction=data.get("instruction", text),
        )
    except (json.JSONDecodeError, ValueError) as e:
        log.warning("Intent classification failed, defaulting to engineering: %s", e)
        return Intent(
            type=IntentType.engineering,
            agent_type="coder",
            instruction=text,
        )
