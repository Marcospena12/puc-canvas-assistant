from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    filters,
)
from telegram.ext import MessageHandler as TelegramMessageHandler

from ..config import UserProfile
from .base import MessageHandler
from .formatting import format_llm_for_telegram, to_telegram_html

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
/sincronizar (ou /sync) - atualiza Canvas e baixa arquivos
/arquivos <matéria> - lista os arquivos conhecidos
/buscar <termo> - pesquisa nos materiais de todas as matérias
/buscar <matéria>: <termo> - pesquisa nos materiais da matéria
/tarefas [matéria] - lista as tarefas pendentes por prazo
/tarefa <n | título> - briefing estruturado para entender e planejar a tarefa
/trecho <n> - exibe o texto completo do resultado N da última busca
/explicar [matéria:] <tópico> - explicação de até ~12 linhas com fontes
/resumo <matéria> <arquivo> [faixa] - resumo estruturado com termos-chave
/quiz <matéria> <tópico> [n] - questões de estudo baseadas no material
/gabarito - respostas comentadas do último quiz
/uso - consumo de tokens de estudo dos últimos 7 dias
/status - estado do cache e falhas recentes
/saude - diagnóstico do sistema, banco, LLM e incidentes
/revisar <n | título> - revisa seu rascunho contra os critérios da tarefa
/testnotify - envia uma notificação de teste
/help - esta ajuda

O bot só responde ao seu usuário/chat configurado."""

MAX_MESSAGE = 3000


def split_text(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
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
        on_health: Callable[[], Awaitable[str]] | None = None,
        on_files: Callable[[str], Awaitable[str]] | None = None,
        on_search: Callable[[str], Awaitable[str]] | None = None,
        on_snippet: Callable[[str], Awaitable[str]] | None = None,
        on_tasks: Callable[[str], Awaitable[str]] | None = None,
        on_task_briefing: Callable[[str], Awaitable[str]] | None = None,
        on_explain: Callable[[str], Awaitable[str]] | None = None,
        on_summary: Callable[[str], Awaitable[str]] | None = None,
        on_quiz: Callable[[str], Awaitable[str]] | None = None,
        on_answer_key: Callable[[], Awaitable[str]] | None = None,
        on_usage: Callable[[], Awaitable[str]] | None = None,
        on_test_notify: Callable[[], Awaitable[str]] | None = None,
        on_review: Callable[[str], Awaitable[str]] | None = None,
        on_review_document: Callable[[object, object], Awaitable[str]] | None = None,
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
        self.on_health = on_health
        self.on_files = on_files
        self.on_search = on_search
        self.on_snippet = on_snippet
        self.on_tasks = on_tasks
        self.on_task_briefing = on_task_briefing
        self.on_explain = on_explain
        self.on_summary = on_summary
        self.on_quiz = on_quiz
        self.on_answer_key = on_answer_key
        self.on_usage = on_usage
        self.on_test_notify = on_test_notify
        self.on_review = on_review
        self.on_review_document = on_review_document
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

    async def _keep_typing(self, update: Update, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self._typing(update)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=4.0)

    async def _reply(self, update: Update, text: str) -> None:
        message = update.effective_message
        if message is None:
            return
        for chunk in split_text(text):
            try:
                await message.reply_text(
                    to_telegram_html(chunk),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except BadRequest:
                logger.warning("Telegram rejeitou HTML; reenviando como texto puro")
                try:
                    await message.reply_text(chunk, disable_web_page_preview=True)
                except Exception:
                    logger.exception("Falha ao enviar mensagem no Telegram")
                    return
            except Exception:
                logger.exception("Falha ao enviar mensagem no Telegram")
                return

    async def send_message(self, user: UserProfile, text: str) -> bool:
        chat_id = user.telegram_chat_id or self.chat_id
        if chat_id is None or self.app is None:
            logger.error("Telegram sem chat configurado ou bot não iniciado")
            return False
        sent_any = False
        for chunk in split_text(text):
            try:
                await self.app.bot.send_message(
                    chat_id=chat_id,
                    text=to_telegram_html(chunk),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                sent_any = True
            except BadRequest:
                logger.warning("Telegram rejeitou HTML; reenviando como texto puro")
                try:
                    await self.app.bot.send_message(
                        chat_id=chat_id,
                        text=chunk,
                        disable_web_page_preview=True,
                    )
                    sent_any = True
                except Exception:
                    logger.exception("Falha ao enviar mensagem proativa no Telegram")
                    return sent_any
            except Exception:
                logger.exception("Falha ao enviar mensagem proativa no Telegram")
                return sent_any
        return sent_any

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

    async def _cmd_files(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_files is None:
            await self._reply(update, "Comando de arquivos indisponível.")
            return
        arg = " ".join(context.args) if context.args else ""
        await self._typing(update)
        try:
            text = await self.on_files(arg)
        except Exception:
            logger.exception("Falha no /arquivos")
            text = "Não consegui listar os arquivos agora."
        await self._reply(update, text)

    async def _cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_search is None:
            await self._reply(update, "Busca indisponível.")
            return
        arg = " ".join(context.args) if context.args else ""
        await self._typing(update)
        try:
            text = await self.on_search(arg)
        except Exception:
            logger.exception("Falha no /buscar")
            text = "Não consegui realizar a busca agora."
        await self._reply(update, text)

    async def _cmd_snippet(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_snippet is None:
            await self._reply(update, "Visualização de trecho indisponível.")
            return
        arg = " ".join(context.args) if context.args else ""
        await self._typing(update)
        try:
            text = await self.on_snippet(arg)
        except Exception:
            logger.exception("Falha no /trecho")
            text = "Não consegui carregar o trecho."
        await self._reply(update, text)

    async def _cmd_tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_tasks is None:
            await self._reply(update, "Consulta de tarefas pendentes indisponível.")
            return
        arg = " ".join(context.args) if context.args else ""
        await self._typing(update)
        try:
            text = await self.on_tasks(arg)
        except Exception:
            logger.exception("Falha no /tarefas")
            text = "Não consegui consultar as tarefas pendentes agora."
        await self._reply(update, text)

    async def _cmd_task_briefing(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_task_briefing is None:
            await self._reply(update, "Briefing de tarefa indisponível.")
            return
        arg = " ".join(context.args) if context.args else ""
        if not arg:
            await self._reply(
                update,
                "Uso: /tarefa <n | título>\n"
                "Exemplo: /tarefa 1\n"
                "Exemplo: /tarefa Elaboração de projeto",
            )
            return
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(update, stop_typing))
        try:
            text = await self.on_task_briefing(arg)
        except Exception:
            logger.exception("Falha no /tarefa")
            text = "Não consegui gerar o briefing da tarefa agora. Tente novamente mais tarde."
        finally:
            stop_typing.set()
            typing_task.cancel()
        await self._reply(update, format_llm_for_telegram(text))

    async def _cmd_explain(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_explain is None:
            await self._reply(update, "Comando de explicação indisponível.")
            return
        arg = " ".join(context.args) if context.args else ""
        if not arg:
            await self._reply(
                update,
                "Uso: /explicar [matéria:] <tópico>\nExemplo: /explicar BI: modelagem dimensional",
            )
            return
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(update, stop_typing))
        try:
            text = await self.on_explain(arg)
        except Exception:
            logger.exception("Falha no /explicar")
            text = "Não consegui gerar a explicação agora. Tente novamente mais tarde."
        finally:
            stop_typing.set()
            typing_task.cancel()
        await self._reply(update, format_llm_for_telegram(text))

    async def _cmd_summary(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_summary is None:
            await self._reply(update, "Comando de resumo indisponível.")
            return
        arg = " ".join(context.args) if context.args else ""
        if not arg:
            await self._reply(
                update,
                "Uso: /resumo <matéria> <arquivo ou aula> [faixa]\n"
                "Exemplo: /resumo BI Aula02\n"
                "Exemplo: /resumo DAM Aula04 páginas 1-15",
            )
            return
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(update, stop_typing))
        try:
            text = await self.on_summary(arg)
        except Exception:
            logger.exception("Falha no /resumo")
            text = "Não consegui gerar o resumo agora. Tente novamente mais tarde."
        finally:
            stop_typing.set()
            typing_task.cancel()
        await self._reply(update, format_llm_for_telegram(text))

    async def _cmd_quiz(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_quiz is None:
            await self._reply(update, "Comando de quiz indisponível.")
            return
        arg = " ".join(context.args) if context.args else ""
        if not arg:
            await self._reply(
                update,
                "Uso: /quiz <matéria> <tópico> [n]\n"
                "Exemplo: /quiz BI 'modelagem dimensional' 5\n"
                "Exemplo: /quiz DAM 'React Native'",
            )
            return
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(update, stop_typing))
        try:
            text = await self.on_quiz(arg)
        except Exception:
            logger.exception("Falha no /quiz")
            text = "Não consegui gerar o quiz agora. Tente novamente mais tarde."
        finally:
            stop_typing.set()
            typing_task.cancel()
        await self._reply(update, format_llm_for_telegram(text))

    async def _cmd_answer_key(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_answer_key is None:
            await self._reply(update, "Gabarito indisponível.")
            return
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(update, stop_typing))
        try:
            text = await self.on_answer_key()
        except Exception:
            logger.exception("Falha no /gabarito")
            text = "Não consegui recuperar o gabarito."
        finally:
            stop_typing.set()
            typing_task.cancel()
        await self._reply(update, format_llm_for_telegram(text))

    async def _cmd_usage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_usage is None:
            await self._reply(update, "Relatório de uso indisponível.")
            return
        try:
            text = await self.on_usage()
        except Exception:
            logger.exception("Falha no /uso")
            text = "Não consegui montar o relatório de uso agora."
        await self._reply(update, text)

    async def _cmd_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_review is None:
            await self._reply(update, "Revisão de rascunho indisponível.")
            return
        arg = " ".join(context.args) if context.args else ""
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(update, stop_typing))
        try:
            text = await self.on_review(arg)
        except Exception:
            logger.exception("Falha no /revisar")
            text = "Não consegui iniciar a revisão agora. Tente novamente mais tarde."
        finally:
            stop_typing.set()
            typing_task.cancel()
        await self._reply(update, format_llm_for_telegram(text))

    async def _on_document(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_review_document is None:
            return
        doc = update.effective_message.document if update.effective_message else None
        if doc is None:
            return
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(update, stop_typing))
        try:
            text = await self.on_review_document(update, context)
        except Exception:
            logger.exception("Falha ao processar documento de revisão")
            text = "Não consegui processar o arquivo agora."
        finally:
            stop_typing.set()
            typing_task.cancel()
        if text:
            await self._reply(update, format_llm_for_telegram(text))

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        try:
            text = await self.on_status()
        except Exception:
            logger.exception("Falha no /status")
            text = "Não consegui montar o status agora."
        await self._reply(update, text)

    async def _cmd_health(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_health is None:
            await self._reply(update, "Saúde indisponível.")
            return
        try:
            text = await self.on_health()
        except Exception:
            logger.exception("Falha no /saude")
            text = "Não consegui montar o diagnóstico agora."
        await self._reply(update, text)

    async def _cmd_testnotify(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        if self.on_test_notify is None:
            await self._reply(update, "Notificações não estão configuradas.")
            return
        try:
            text = await self.on_test_notify()
        except Exception:
            logger.exception("Falha no /testnotify")
            text = "Não consegui enviar a notificação de teste."
        await self._reply(update, text)

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            logger.debug("Mensagem ignorada de usuário/chat não autorizado")
            return
        text = (update.effective_message.text or "").strip() if update.effective_message else ""
        if not text:
            return
        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(self._keep_typing(update, stop_typing))
        try:
            answer = await asyncio.wait_for(
                self.on_message(self.user, text), timeout=self.handler_timeout
            )
        except TimeoutError:
            answer = "A consulta demorou demais. Tente uma pergunta mais específica."
        except Exception:
            logger.exception("Falha no agente")
            answer = "Tive um problema ao processar sua mensagem. Tente de novo."
        finally:
            stop_typing.set()
            typing_task.cancel()
        if answer:
            await self._reply(update, format_llm_for_telegram(answer))

    async def start(self, handler: MessageHandler) -> None:
        self.on_message = handler
        self.app = Application.builder().token(self.token).concurrent_updates(1).build()
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("sync", self._cmd_sync))
        self.app.add_handler(CommandHandler("sincronizar", self._cmd_sync))
        self.app.add_handler(CommandHandler("arquivos", self._cmd_files))
        self.app.add_handler(CommandHandler("buscar", self._cmd_search))
        self.app.add_handler(CommandHandler("tarefas", self._cmd_tasks))
        self.app.add_handler(CommandHandler("tarefa", self._cmd_task_briefing))
        self.app.add_handler(CommandHandler("trecho", self._cmd_snippet))
        self.app.add_handler(CommandHandler("explicar", self._cmd_explain))
        self.app.add_handler(CommandHandler("resumo", self._cmd_summary))
        self.app.add_handler(CommandHandler("quiz", self._cmd_quiz))
        self.app.add_handler(CommandHandler("gabarito", self._cmd_answer_key))
        self.app.add_handler(CommandHandler("uso", self._cmd_usage))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("saude", self._cmd_health))
        self.app.add_handler(CommandHandler("testnotify", self._cmd_testnotify))
        self.app.add_handler(CommandHandler("revisar", self._cmd_review))
        self.app.add_handler(TelegramMessageHandler(filters.Document.ALL, self._on_document))
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
