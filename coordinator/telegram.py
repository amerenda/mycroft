"""Telegram bot integration — long polling mode (no webhook needed)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

from common.llm import LLMClient
from common.models import IntentType
from coordinator.intent import classify

log = logging.getLogger(__name__)


class TelegramBot:
    """Handles inbound Telegram messages and dispatches to agents.

    Uses long polling — no public endpoint needed. The bot polls
    Telegram's servers for updates in a background task.
    """

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
        self._polling_task: asyncio.Task | None = None

    async def setup(self) -> Application:
        """Initialize the Telegram bot application."""
        self.app = Application.builder().token(self.token).build()
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_message))
        await self.app.initialize()
        return self.app

    async def start_polling(self) -> None:
        """Start polling for updates in a background task."""
        if not self.app:
            return

        # Delete any stale webhook so polling works
        await self.app.bot.delete_webhook(drop_pending_updates=True)

        await self.app.start()
        await self.app.updater.start_polling(
            drop_pending_updates=True,
            allowed_updates=["message"],
        )
        log.info("Telegram bot polling started")

    async def stop_polling(self) -> None:
        """Stop polling."""
        if self.app and self.app.updater and self.app.updater.running:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            log.info("Telegram bot polling stopped")

    async def _handle_message(self, update: Update, context) -> None:
        if not update.message or not update.message.text:
            return

        # Only respond to the configured chat
        if str(update.message.chat_id) != self.chat_id:
            log.warning("Ignoring message from unauthorized chat: %s", update.message.chat_id)
            return

        text = update.message.text
        log.info("Telegram message: %s", text[:100])

        try:
            intent = await classify(text, self.llm)

            if intent.type in (IntentType.engineering, IntentType.research):
                agent = intent.agent_type or ("researcher" if intent.type == IntentType.research else "coder")
                # Map effort to max_iterations for research tasks
                effort_map = {"light": 5, "regular": 15, "deep": 25}
                max_iter = effort_map.get(intent.effort or "regular") if agent == "researcher" else None
                try:
                    task_id = await self._on_engineering_task(
                        instruction=intent.instruction,
                        agent_type=agent,
                        repo=intent.repo or "",
                        max_iterations=max_iter,
                    )
                    await update.message.reply_text(f"On it. {agent} task {task_id[:8]} launched.")
                except Exception as e:
                    log.error("Failed to launch task: %s", e)
                    await update.message.reply_text(f"Task created but launch failed: {e}")

            elif intent.type == IntentType.system:
                response = await self._on_status_query(text)
                await update.message.reply_text(response)

            else:
                # general — Phase 1c (OpenClaw)
                await update.message.reply_text(
                    "General assistant not available yet (Phase 1c). "
                    "I can handle engineering tasks and status queries."
                )
        except Exception as e:
            log.exception("Error handling Telegram message")
            await update.message.reply_text(f"Error: {e}")

    async def send(self, text: str) -> None:
        """Send a message to Alex."""
        if self.app and self.app.bot:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text)
