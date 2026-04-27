"""Telegram bot integration — long polling mode (no webhook needed)."""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, MessageHandler, filters

log = logging.getLogger(__name__)


class TelegramBot:
    """Handles inbound Telegram messages and dispatches to agents.

    Uses long polling — no public endpoint needed. The bot polls
    Telegram's servers for updates in a background task.

    Intent-based routing is disabled. Submit tasks via the Agent Studio UI.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
    ):
        self.token = token
        self.chat_id = chat_id
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
        log.info("Telegram message (unrouted): %s", text[:100])
        await update.message.reply_text(
            "Submit tasks via Agent Studio: https://mycroft.amer.dev"
        )

    async def send(self, text: str) -> None:
        """Send a message to Alex."""
        if self.app and self.app.bot:
            await self.app.bot.send_message(chat_id=self.chat_id, text=text)
