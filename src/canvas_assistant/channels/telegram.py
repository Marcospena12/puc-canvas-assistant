from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    filters,
)
from telegram.ext import MessageHandler as TelegramMessageHandler

from ..config import UserProfile
from .base import MessageHandler

logger = logging.getLogger(__name__)

HELP_TEXT = """Assistente acadêmico do Canvas.

Pergunte em linguagem natural:
- o que tenho pra hoje?
- o que vence essa semana?
- como estão minhas notas?
- quais matérias estou cursando?
- tem aviso novo?
- quais arquivos tem em Cálculo?

Comandos:
/sync - atualiza os dados do Canvas
/status - estado do cache e falhas recentes
/help - esta ajuda

O bot só responde ao seu usuário/chat configurado."""

MAX_MESSAGE = 4000


def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


class TelegramChannel:
    def __init__(
        self,
        token: str,
        *,
        user: UserProfile,
        chat_id: int | None,
        allowed_user_ids: frozenset[int],
        on_message: MessageHandler,
        on_sync: Callable[[], Awaitable[str]],
        on_status: Callable[[], Awaitable[str]],
        handler_timeout: float = 240.0,
    ) -> None:
        if chat_id is None and not allowed_user_ids:
            raise ValueError(
                "Configure TELEGRAM_CHAT_ID e/ou TELEGRAM_ALLOWED_USER_IDS para autorizar o bot"
            )
        self.token = token
        self.user = user
        self.chat_id = chat_id
        self.allowed_user_ids = allowed_user_ids
        self.on_message = on_message
        self.on_sync = on_sync
        self.on_status = on_status
        self.handler_timeout = handler_timeout
        self.app: Application | None = None
        self._stop_event: asyncio.Event | None = None
        self._sync_lock = asyncio.Lock()

    def _authorized(self, update: Update) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:
            return False
        if self.allowed_user_ids and user.id not in self.allowed_user_ids:
            return False
        if self.chat_id is not None and chat.id != self.chat_id:
            return False
        return bool(self.allowed_user_ids) or self.chat_id is not None

    async def _typing(self, update: Update) -> None:
        try:
            if update.effective_chat:
                await update.effective_chat.send_action(ChatAction.TYPING)
        except Exception:
            logger.debug("Não foi possível enviar ação de digitando", exc_info=True)

    async def _reply(self, update: Update, text: str) -> None:
        message = update.effective_message
        if message is None:
            return
        for chunk in split_text(text):
            try:
                await message.reply_text(chunk, disable_web_page_preview=True)
            except Exception:
                logger.exception("Falha ao enviar mensagem no Telegram")
                return

    async def send_message(self, user: UserProfile, text: str) -> None:
        chat_id = user.telegram_chat_id or self.chat_id
        if chat_id is None or self.app is None:
            logger.error("Telegram sem chat configurado ou bot não iniciado")
            return
        for chunk in split_text(text):
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    disable_web_page_preview=True,
                )
            except Exception:
                logger.exception("Falha ao enviar mensagem proativa no Telegram")

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._reply(update, HELP_TEXT)

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._reply(update, HELP_TEXT)

    async def _cmd_sync(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self._sync_lock.locked():
            await self._reply(update, "Já estou sincronizando. Aguarde um instante.")
            return
        await self._reply(update, "Sincronizando com o Canvas…")
        await self._typing(update)
        async with self._sync_lock:
            try:
                text = await asyncio.wait_for(self.on_sync(), timeout=self.handler_timeout)
            except TimeoutError:
                text = "A sincronização demorou demais. Tente novamente em instantes."
            except Exception:
                logger.exception("Falha no /sync")
                text = "Não consegui sincronizar agora. Veja os logs para detalhes."
        await self._reply(update, text)

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        try:
            text = await self.on_status()
        except Exception:
            logger.exception("Falha no /status")
            text = "Não consegui montar o status agora."
        await self._reply(update, text)

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            logger.debug("Mensagem ignorada de usuário/chat não autorizado")
            return
        text = (update.effective_message.text or "").strip() if update.effective_message else ""
        if not text:
            return
        await self._typing(update)
        try:
            answer = await asyncio.wait_for(
                self.on_message(self.user, text), timeout=self.handler_timeout
            )
        except TimeoutError:
            answer = "A consulta demorou demais. Tente uma pergunta mais específica."
        except Exception:
            logger.exception("Falha no agente")
            answer = "Tive um problema ao processar sua mensagem. Tente de novo."
        if answer:
            await self._reply(update, answer)

    async def start(self, handler: MessageHandler) -> None:
        self.on_message = handler
        self.app = Application.builder().token(self.token).concurrent_updates(1).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("sync", self._cmd_sync))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(TelegramMessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))
        self._stop_event = asyncio.Event()

        await self.app.initialize()
        await self.app.start()
        if self.app.updater is None:
            raise RuntimeError("Telegram updater indisponível")
        await self.app.updater.start_polling(allowed_updates=[Update.MESSAGE])
        logger.info("Bot do Telegram iniciado")
        try:
            await self._stop_event.wait()
        finally:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    async def stop(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
