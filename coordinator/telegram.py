"""Telegram bot integration — inbound message handling."""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from common.llm import LLMClient
from common.models import IntentType
from coordinator.intent import classify

log = logging.getLogger(__name__)


class TelegramBot:
    """Handles inbound Telegram messages and dispatches to agents."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        llm: LLMClient,
        on_engineering_task,  # async callable(instruction, agent_type, repo) -> task_id
        on_status_query,     # async callable(text) -> str
    ):
        self.token = token
        self.chat_id = chat_id
        self.llm = llm
        self._on_engineering_task = on_engineering_task
        self._on_status_query = on_status_query
        self.app: Application | None = None

    async def setup(self) -> Application:
        """Initialize the Telegram bot application."""
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        await self.app.initialize()
        return self.app

    async def _handle_message(self, update: Update, context) -> None:
        if not update.message or not update.message.text:
            return

        # Only respond to the configured chat
        if str(update.message.chat_id) != self.chat_id:
            log.warning("Ignoring message from unauthorized chat: %s", update.message.chat_id)
            return

        text = update.message.text
        log.info("Telegram message: %s", text[:100])

        intent = await classify(text, self.llm)

        if intent.type == IntentType.engineering:
            task_id = await self._on_engineering_task(
                instruction=intent.instruction,
                agent_type=intent.agent_type or "coder",
                repo=intent.repo or "",
            )
            await update.message.reply_text(f"On it. Task {task_id[:8]} launched.")

        elif intent.type == IntentType.system:
            response = await self._on_status_query(text)
            await update.message.reply_text(response)

        else:
            # general — Phase 1c (OpenClaw)
            await update.message.reply_text(
                "General assistant not available yet (Phase 1c). "
                "I can handle engineering tasks and status queries."
            )

    async def send(self, text: str) -> None:
        """Send a message to Alex."""
        if self.app and self.app.bot:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text)
