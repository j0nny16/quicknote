"""Telegram capture via long polling.

Long polling is a deliberate choice over webhooks: the bot dials out, so the VPS
needs no inbound port, no reverse proxy and no TLS certificate.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..models import RawItem

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 45 * 1024 * 1024


class TelegramSource:
    name = "telegram"

    def __init__(
        self,
        token: str,
        *,
        allowed_user_ids: list[int],
        audio_dir: Path,
        on_item: Callable[[RawItem], Awaitable[None]],
        on_undo: Callable[[str], Awaitable[str]],
        on_status: Callable[[], Awaitable[str]],
        ack_emoji: str = "⏳",
    ) -> None:
        if not allowed_user_ids:
            raise ValueError(
                "source.allowed_user_ids is empty -- refusing to start an open bot. "
                "Add your numeric Telegram user id (ask @userinfobot)."
            )
        self.allowed = set(allowed_user_ids)
        self.audio_dir = Path(audio_dir)
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.on_item = on_item
        self.on_undo = on_undo
        self.on_status = on_status
        self.ack_emoji = ack_emoji

        self.app: Application = Application.builder().token(token).build()
        allow = filters.User(user_id=list(self.allowed))
        self.app.add_handler(CommandHandler("start", self._cmd_start, filters=allow))
        self.app.add_handler(CommandHandler("help", self._cmd_start, filters=allow))
        self.app.add_handler(CommandHandler("undo", self._cmd_undo, filters=allow))
        self.app.add_handler(CommandHandler("status", self._cmd_status, filters=allow))
        self.app.add_handler(
            MessageHandler(allow & (filters.VOICE | filters.AUDIO), self._on_voice)
        )
        self.app.add_handler(
            MessageHandler(allow & filters.TEXT & ~filters.COMMAND, self._on_text)
        )
        # Anything from anyone else is dropped silently: replying would confirm
        # the bot exists to whoever found it.
        self.app.add_handler(MessageHandler(~allow, self._on_stranger))

    # -- handlers --

    async def _cmd_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(
            "QuickNotes\n\n"
            "Send a voice message or type a note -- it lands in Anytype.\n\n"
            "/undo - remove the last saved note\n"
            "/status - queue and backend status"
        )

    async def _cmd_undo(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self.on_undo(str(update.effective_chat.id)))

    async def _cmd_status(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await update.message.reply_text(await self.on_status())

    async def _on_stranger(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        log.warning(
            "ignored message from non-whitelisted user id=%s username=%s",
            getattr(user, "id", "?"),
            getattr(user, "username", "?"),
        )

    async def _on_text(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.message.text or "").strip()
        if not text:
            return
        await self._accept(update, RawItem(source=self.name, text=text))

    async def _on_voice(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        media = update.message.voice or update.message.audio
        if media is None:
            return
        if (media.file_size or 0) > MAX_AUDIO_BYTES:
            await update.message.reply_text("That recording is too large for me to fetch.")
            return

        await ctx.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        tg_file = await ctx.bot.get_file(media.file_id)
        suffix = Path(getattr(media, "file_name", "") or "voice.ogg").suffix or ".ogg"
        path = self.audio_dir / f"{uuid.uuid4().hex}{suffix}"
        await tg_file.download_to_drive(custom_path=path)

        await self._accept(
            update,
            RawItem(
                source=self.name,
                audio_path=str(path),
                audio_mime=media.mime_type or "audio/ogg",
                caption=(update.message.caption or "").strip() or None,
            ),
        )

    async def _accept(self, update: Update, item: RawItem) -> None:
        """Acknowledge immediately, then hand the capture to the queue."""
        ack = await update.message.reply_text(f"{self.ack_emoji} queued…")
        item.meta = {
            "chat_id": update.effective_chat.id,
            "chat_key": str(update.effective_chat.id),
            "ack_message_id": ack.message_id,
        }
        try:
            await self.on_item(item)
        except Exception as exc:
            log.exception("failed to enqueue capture")
            await ack.edit_text(f"❌ could not queue this note: {exc}")

    # -- Source protocol --

    async def notify(self, item: RawItem, text: str) -> None:
        chat_id = item.meta.get("chat_id")
        message_id = item.meta.get("ack_message_id")
        if chat_id is None:
            return
        try:
            if message_id:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id, message_id=message_id, text=text
                )
            else:
                await self.app.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            log.exception("could not deliver notification to chat %s", chat_id)

    async def run(self) -> None:
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=False)
        log.info("telegram polling started for user ids %s", sorted(self.allowed))
        try:
            await asyncio.Event().wait()  # until cancelled
        finally:
            await self.aclose()

    async def health(self) -> str:
        bot = await self.app.bot.get_me()
        return f"@{bot.username} reachable, whitelist {sorted(self.allowed)}"

    async def aclose(self) -> None:
        try:
            if self.app.updater and self.app.updater.running:
                await self.app.updater.stop()
            if self.app.running:
                await self.app.stop()
            await self.app.shutdown()
        except Exception:
            log.exception("error during telegram shutdown")
