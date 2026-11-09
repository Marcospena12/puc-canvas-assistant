from __future__ import annotations

import asyncio
import logging

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .agent import Agent
from .canvas import CanvasClient, CanvasService
from .channels import TelegramChannel
from .config import Settings
from .db import Database
from .llm import create_provider
from .repository import Repository
from .sync import SyncResult, SyncService
from .timeutil import format_local, get_timezone

logger = logging.getLogger(__name__)


class SyncRunner:
    """Garante que apenas uma sincronização rode por vez."""

    def __init__(self, sync: SyncService) -> None:
        self.sync = sync
        self.lock = asyncio.Lock()

    async def run(self, *, block: bool = True) -> SyncResult | None:
        if not block and self.lock.locked():
            return None
        async with self.lock:
            return await self.sync.sync_all()


def format_sync_result(result: SyncResult) -> str:
    lines = ["Sincronização concluída."]
    lines.extend(f"- {line}" for line in result.as_lines())
    if not result.issues:
        lines.append("Nenhuma falha registrada.")
    return "\n".join(lines)


def build_status_text(repo: Repository, settings: Settings) -> str:
    user = settings.user
    tz = get_timezone(settings.timezone)
    stats = repo.cache_stats(user.user_id)
    issues = repo.list_issues(user.user_id, limit=5)

    labels = {
        "courses": "cursos",
        "assignments": "tarefas",
        "planner_items": "itens do planner",
        "announcements": "avisos",
        "files": "arquivos",
        "modules": "módulos",
    }
    lines = [
        "Status do assistente",
        f"Modelo: {settings.llm_provider} / {settings.llm_model}",
    ]
    for key, label in labels.items():
        info = stats.get(key, {})
        lines.append(f"- {label}: {info.get('count', 0)}")
    last_times = [
        info.get("last_synced_at") for info in stats.values() if info.get("last_synced_at")
    ]
    if last_times:
        lines.append(f"Última atualização: {format_local(max(last_times), tz)}")
    if issues:
        lines.append("Falhas recentes:")
        for issue in issues:
            scope = f" (curso {issue['scope_id']})" if issue.get("scope_id") else ""
            lines.append(f"- {issue['resource']}{scope}: {issue['message']}")
    else:
        lines.append("Nenhuma falha recente.")
    return "\n".join(lines)


async def run_bot(settings: Settings) -> None:
    user = settings.user
    tz = get_timezone(settings.timezone)

    if settings.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN não configurado. Preencha o .env para rodar o bot.")
    if user.telegram_chat_id is None and not user.telegram_user_ids:
        raise SystemExit(
            "Configure TELEGRAM_CHAT_ID e/ou TELEGRAM_ALLOWED_USER_IDS para autorizar o bot."
        )

    database = Database(settings.db_path)
    database.initialize()
    repo = Repository(database)

    async with httpx.AsyncClient() as http:
        canvas = CanvasService(CanvasClient(http, user.canvas_base_url, user.canvas_token))
        sync = SyncService(
            canvas,
            repo,
            user,
            tz,
            past_days=settings.sync_past_days,
            future_days=settings.sync_future_days,
            announcements_lookback_days=settings.announcements_lookback_days,
        )
        runner = SyncRunner(sync)
        provider = create_provider(settings.llm)
        agent = Agent(provider, repo, user, tz)
        scheduler = AsyncIOScheduler(timezone=tz)

        async def refresh_job() -> None:
            result = await runner.run(block=False)
            if result is not None:
                logger.info(
                    "Refresh periódico: %s cursos, %s tarefas, %s avisos",
                    result.courses,
                    result.assignments,
                    result.announcements,
                )

        async def on_message(_user: object, text: str) -> str:
            return await agent.reply(text)

        async def on_sync() -> str:
            result = await runner.run(block=False)
            if result is None:
                return "Já existe uma sincronização em andamento."
            return format_sync_result(result)

        async def on_status() -> str:
            return build_status_text(repo, settings)

        channel = TelegramChannel(
            settings.telegram_bot_token.get_secret_value(),
            user=user,
            chat_id=user.telegram_chat_id,
            allowed_user_ids=user.telegram_user_ids,
            on_message=on_message,
            on_sync=on_sync,
            on_status=on_status,
            handler_timeout=settings.llm_timeout_seconds + 60,
        )

        try:
            logger.info("Sincronização inicial…")
            try:
                initial = await runner.run(block=True)
                if initial is not None:
                    logger.info("Sincronização inicial: %s cursos", initial.courses)
                    for issue in initial.issues:
                        logger.warning("Falha na sincronização inicial: %s", issue)
            except Exception as exc:
                logger.exception("Falha na sincronização inicial")
                repo.add_issue(user.user_id, "sync", str(exc), 0, getattr(exc, "status_code", None))

            scheduler.add_job(
                refresh_job,
                "interval",
                minutes=settings.poll_interval_minutes,
                id="refresh_canvas",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )
            scheduler.start()
            logger.info("Refresh automático a cada %s min", settings.poll_interval_minutes)
            await channel.start(on_message)
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            await agent.aclose()
