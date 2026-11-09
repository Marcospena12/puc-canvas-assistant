from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .agent import Agent
from .canvas import CanvasClient, CanvasService
from .channels import TelegramChannel
from .config import Settings
from .db import Database
from .knowledge.downloader import FileDownloader
from .knowledge.file_sync import FileSyncResult, FileSyncService
from .knowledge.report import (
    build_files_text,
    format_file_sync_result,
    get_user_snippet,
    perform_bot_search,
)
from .llm import create_provider
from .notifications import NotificationService
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


class FileSyncRunner:
    """Garante que apenas uma sincronização de arquivos rode por vez."""

    def __init__(self, file_sync: FileSyncService) -> None:
        self.file_sync = file_sync
        self.lock = asyncio.Lock()

    async def run(self, *, block: bool = True, download: bool = True) -> FileSyncResult | None:
        if not block and self.lock.locked():
            return None
        async with self.lock:
            return await self.file_sync.sync_all(download=download)


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

    file_stats = repo.file_sync_stats(user.user_id)
    if file_stats.get("total", 0) > 0 or file_stats.get("blocked_courses", 0) > 0:
        lines.append("Base de arquivos:")
        lines.append(
            f"- total catalogado: {file_stats['total']} | baixados: {file_stats['downloaded']}"
        )
        lines.append(
            f"- pendentes: {file_stats['pending']} | "
            f"ignorados: {file_stats['ignored']} | falhas: {file_stats['failed']}"
        )
        if file_stats.get("bytes_downloaded"):
            mb = file_stats["bytes_downloaded"] / (1024 * 1024)
            lines.append(f"- volume baixado: {mb:.1f} MB")
        if file_stats.get("blocked_courses"):
            lines.append(f"- matérias com aba bloqueada: {file_stats['blocked_courses']}")

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
            file_blocked_retry_hours=settings.file_blocked_retry_hours,
        )
        runner = SyncRunner(sync)

        file_downloader = FileDownloader(
            http,
            user.canvas_base_url,
            user.canvas_token,
            timeout=settings.file_download_timeout_seconds,
        )
        file_sync = FileSyncService(
            repo,
            canvas,
            user,
            tz,
            settings.file_sync,
            file_downloader,
        )
        file_runner = FileSyncRunner(file_sync)

        provider = create_provider(settings.llm)
        agent = Agent(provider, repo, user, tz)
        scheduler = AsyncIOScheduler(timezone=tz)
        notifications: NotificationService | None = None

        async def refresh_job() -> None:
            result = await runner.run(block=False)
            if result is not None:
                logger.info(
                    "Refresh periódico: %s cursos, %s tarefas, %s avisos",
                    result.courses,
                    result.assignments,
                    result.announcements,
                )
                if notifications is not None:
                    notifications.mark_sync_success()
                    await notifications.run_cycle()

        async def file_sync_job() -> None:
            if not settings.file_sync_enabled:
                return
            result = await file_runner.run(block=False, download=True)
            if result is not None:
                logger.info(
                    "Sincronização periódica de arquivos: %s vistos, %s baixados, %s ignorados",
                    result.files_seen,
                    result.downloaded,
                    result.ignored,
                )
                if notifications is not None:
                    await notifications.run_cycle()

        async def on_message(_user: object, text: str) -> str:
            return await agent.reply(text)

        async def on_sync() -> str:
            repo.clear_file_retries(user.user_id)
            result = await runner.run(block=False)
            if result is None:
                return "Já existe uma sincronização em andamento."

            file_result: FileSyncResult | None = None
            if settings.file_sync_enabled:
                file_result = await file_runner.run(block=False, download=True)

            if notifications is not None:
                notifications.mark_sync_success()
                await notifications.run_cycle()

            output = [format_sync_result(result)]
            if file_result is not None:
                output.append("")
                output.append(format_file_sync_result(file_result))
            elif settings.file_sync_enabled:
                output.append("")
                output.append("A sincronização de arquivos já estava em andamento em background.")
            return "\n".join(output)

        async def on_files(query: str) -> str:
            return build_files_text(repo, user, tz, query)

        async def on_search(query: str) -> str:
            return perform_bot_search(repo, user.user_id, query)

        async def on_snippet(arg: str) -> str:
            return get_user_snippet(user.user_id, arg)

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
            on_files=on_files,
            on_search=on_search,
            on_snippet=on_snippet,
            on_test_notify=notifications.send_test if notifications else None,
            handler_timeout=settings.llm_timeout_seconds + 60,
        )
        notifications = NotificationService(repo, user, tz, channel, settings.notification)
        channel.on_test_notify = notifications.send_test

        try:
            logger.info("Sincronização inicial…")
            try:
                initial = await runner.run(block=True)
                if initial is not None:
                    logger.info("Sincronização inicial: %s cursos", initial.courses)
                    if settings.file_sync_enabled:
                        try:
                            await file_runner.run(block=True, download=False)
                        except Exception:
                            logger.exception("Falha no catálogo inicial de arquivos")
                    notifications.mark_sync_success()
                    notifications.initialize()
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
            if settings.file_sync_enabled:
                scheduler.add_job(
                    file_sync_job,
                    "interval",
                    minutes=settings.file_sync_interval_minutes,
                    id="file_sync",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=300,
                    next_run_time=datetime.now(tz) + timedelta(seconds=15),
                )
            scheduler.add_job(
                notifications.run_cycle,
                "interval",
                minutes=settings.notify_check_minutes,
                id="notifications",
                max_instances=1,
                coalesce=True,
                misfire_grace_time=120,
            )
            scheduler.start()
            logger.info("Refresh automático a cada %s min", settings.poll_interval_minutes)
            if settings.file_sync_enabled:
                logger.info(
                    "Sincronização de arquivos a cada %s min",
                    settings.file_sync_interval_minutes,
                )
            await channel.start(on_message)
        finally:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            await agent.aclose()
