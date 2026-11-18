from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .agent import Agent
from .canvas import CanvasClient, CanvasService
from .canvas.errors import CanvasAuthError, CanvasError, CanvasForbiddenError
from .catchup import (
    build_catchup_message,
    get_last_cycle,
    mark_catchup_sent,
    mark_scheduler_cycle,
    persist_started_at,
    should_run_catchup,
)
from .channels import TelegramChannel
from .config import Settings
from .db import Database
from .incidents import IncidentAwareProvider, IncidentService
from .knowledge.content_sync import ContentSyncResult, ContentSyncService
from .knowledge.downloader import FileDownloader
from .knowledge.file_sync import FileSyncResult, FileSyncService
from .knowledge.report import (
    build_files_text,
    format_file_sync_result,
    get_user_snippet,
    perform_bot_search,
)
from .knowledge.study_service import (
    explain_topic,
    generate_quiz,
    get_quiz_answer_key,
    get_usage_report,
    summarize_file,
)
from .knowledge.task_briefing import (
    format_refusal_message,
    generate_task_briefing,
    is_resolution_request,
    list_pending_tasks_text,
    resolve_task_selection,
)
from .llm import create_provider, create_study_provider
from .notifications import NotificationService
from .repository import Repository
from .sync import SyncResult, SyncService
from .textutil import short_course_name
from .timeutil import format_local, get_timezone, utc_now

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

    study_summary = repo.get_study_usage_summary(user.user_id, days=7)
    if study_summary:
        lines.append("Uso de estudo (últimos 7 dias):")
        for row in study_summary:
            tot = (row["total_input_tokens"] or 0) + (row["total_output_tokens"] or 0)
            lines.append(f"- /{row['command']}: {row['executions']} execs | {tot:,} tokens")

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


def build_health_text(
    repo: Repository,
    settings: Settings,
    *,
    incidents: IncidentService | None = None,
    started_at: datetime | None = None,
) -> str:
    user = settings.user
    tz = get_timezone(settings.timezone)
    now = utc_now()
    lines = ["Saúde do assistente"]

    size_bytes = settings.db_path.stat().st_size if settings.db_path.exists() else 0
    integrity = "n/a"
    last_content = last_files = None
    conn = repo.db.connect()
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        integrity = str(row[0]) if row else "n/a"
        content_row = conn.execute(
            "SELECT MAX(last_success_at) AS last FROM content_sync_state WHERE user_id = ?",
            (user.user_id,),
        ).fetchone()
        last_content = content_row["last"] if content_row else None
        files_row = conn.execute(
            "SELECT MAX(last_files_attempt_at) AS last FROM file_course_sync WHERE user_id = ?",
            (user.user_id,),
        ).fetchone()
        last_files = files_row["last"] if files_row else None
    finally:
        conn.close()

    lines.append(
        f"- banco: {size_bytes / (1024 * 1024):.1f} MB | integrity={integrity} | "
        f"schema={repo.get_meta('version') or '?'}"
    )
    lines.append(f"- modelo ativo: {settings.llm_provider} / {settings.llm.model}")
    stats = repo.cache_stats(user.user_id)
    last_meta = max(
        (info.get("last_synced_at") for info in stats.values() if info.get("last_synced_at")),
        default=None,
    )
    lines.append(f"- último sync de metadados: {format_local(last_meta, tz)}")
    lines.append(f"- último sync de conteúdo: {format_local(last_content, tz)}")
    lines.append(f"- última tentativa de arquivos: {format_local(last_files, tz)}")
    last_cycle = get_last_cycle(repo, user.user_id)
    lines.append(f"- último ciclo do scheduler: {format_local(last_cycle, tz)}")

    latest = repo.get_latest_study_usage(user.user_id)
    if latest:
        lines.append(
            f"- último LLM: /{latest['command']} em {format_local(latest['created_at'], tz)} | "
            f"{latest['latency_ms']}ms | {latest['input_tokens']} in / "
            f"{latest['output_tokens']} out"
        )
    else:
        lines.append("- último LLM: sem execuções registradas")

    if incidents is not None:
        status = incidents.status()
        active = [
            name
            for name, key in (
                ("Canvas", "canvas_active"),
                ("autenticação Canvas", "canvas_auth_active"),
                ("LLM", "llm_active"),
            )
            if status.get(key)
        ]
        lines.append(
            f"- incidentes ativos: {', '.join(active) if active else 'nenhum'} | "
            f"falhas LLM seguidas: {status.get('llm_failures', 0)}"
        )

    issues = repo.list_issues(user.user_id, limit=5)
    if issues:
        lines.append("Falhas recentes:")
        for issue in issues:
            scope = f" (curso {issue['scope_id']})" if issue.get("scope_id") else ""
            lines.append(f"- {issue['resource']}{scope}: {issue['message']}")
    else:
        lines.append("- falhas recentes: nenhuma")

    if started_at is not None:
        elapsed = max(0, int((now - started_at).total_seconds()))
        hours, remainder = divmod(elapsed, 3600)
        minutes = remainder // 60
        lines.append(f"- uptime: {hours}h {minutes}min")
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
    started_at = utc_now()
    persist_started_at(repo, user.user_id, started_at)
    incidents = IncidentService(repo, user, None, settings.incidents)

    async def record_canvas_exception(exc: Exception) -> None:
        if isinstance(exc, (CanvasAuthError, CanvasForbiddenError)):
            await incidents.canvas_auth_failure()
        elif isinstance(exc, CanvasError):
            await incidents.canvas_failure()

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
        content_sync = ContentSyncService(
            repo,
            canvas,
            user,
            tz,
            blocked_retry_hours=settings.content_blocked_retry_hours,
        )

        provider = IncidentAwareProvider(create_provider(settings.llm), incidents)
        study_provider = IncidentAwareProvider(create_study_provider(settings), incidents)
        agent = Agent(provider, repo, user, tz)
        scheduler = AsyncIOScheduler(timezone=tz)
        notifications: NotificationService | None = None

        async def refresh_job() -> None:
            try:
                result = await runner.run(block=False)
            except Exception as exc:
                logger.exception("Falha no refresh periódico")
                await record_canvas_exception(exc)
                return
            if result is not None:
                await incidents.canvas_success()
                mark_scheduler_cycle(repo, user.user_id)
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
            try:
                result = await file_runner.run(block=False, download=True)
            except Exception as exc:
                logger.exception("Falha na sincronização periódica de arquivos")
                await record_canvas_exception(exc)
                return
            if result is not None:
                await incidents.canvas_success()
                mark_scheduler_cycle(repo, user.user_id)
                logger.info(
                    "Sincronização periódica de arquivos: %s vistos, %s baixados, %s ignorados",
                    result.files_seen,
                    result.downloaded,
                    result.ignored,
                )
                if notifications is not None:
                    await notifications.run_cycle()

        async def content_sync_job() -> None:
            if not settings.content_sync_enabled:
                return
            try:
                c_res = await content_sync.sync_all(notify_baseline=True)
            except Exception as exc:
                logger.exception("Falha na sincronização periódica de conteúdo")
                await record_canvas_exception(exc)
                return
            await incidents.canvas_success()
            mark_scheduler_cycle(repo, user.user_id)
            if c_res.chunks_created > 0 or c_res.events:
                logger.info(
                    "Sincronização periódica de conteúdo: %s páginas, %s tarefas (%s chunks)",
                    c_res.pages_ingested,
                    c_res.assignments_ingested,
                    c_res.chunks_created,
                )
                if notifications is not None and c_res.events:
                    await notifications.send_content_events(c_res.events)

        async def heartbeat_job() -> None:
            if not settings.heartbeat_url:
                return
            last = get_last_cycle(repo, user.user_id)
            if last is None:
                return
            try:
                await http.get(settings.heartbeat_url, timeout=10.0)
            except Exception:
                logger.warning("Heartbeat falhou para %s", settings.heartbeat_url)

        async def on_message(_user: object, text: str) -> str:
            return await agent.reply(text)

        async def on_sync() -> str:
            repo.clear_file_retries(user.user_id)
            repo.clear_content_retries(user.user_id)
            try:
                result = await runner.run(block=False)
            except Exception as exc:
                logger.exception("Falha na sincronização manual")
                await record_canvas_exception(exc)
                return f"Falha na sincronização: {exc}"
            if result is None:
                return "Jǭ existe uma sincroniza��ǜo em andamento."
            await incidents.canvas_success()

            file_result: FileSyncResult | None = None
            if settings.file_sync_enabled:
                file_result = await file_runner.run(block=False, download=True)

            c_res: ContentSyncResult | None = None
            if settings.content_sync_enabled:
                c_res = await content_sync.sync_all(notify_baseline=True)
                if notifications is not None and c_res.events:
                    await notifications.send_content_events(c_res.events)

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

            if c_res is not None:
                output.append("")
                output.append(
                    f"Conteúdo do Canvas: {c_res.pages_ingested} páginas, "
                    f"{c_res.assignments_ingested} tarefas, {c_res.announcements_ingested} avisos "
                    f"({c_res.chunks_created} novos chunks)."
                )
            return "\n".join(output)

        async def on_files(query: str) -> str:
            return build_files_text(repo, user, tz, query)

        async def on_search(query: str) -> str:
            return perform_bot_search(repo, user.user_id, query, tz=tz)

        async def on_snippet(arg: str) -> str:
            return get_user_snippet(user.user_id, arg)

        async def on_tasks(arg: str) -> str:
            return list_pending_tasks_text(repo, user.user_id, tz, course_query=arg)

        async def on_task_briefing(arg: str) -> str:
            assignment, candidates, err = resolve_task_selection(repo, user.user_id, arg, tz)
            if err:
                return err
            if candidates:
                cand_lines = [
                    f"- <b>{c.get('name')}</b> ({short_course_name(c.get('course_name'))})"
                    for c in candidates
                ]
                return (
                    f"Mais de uma tarefa corresponde a '{arg}':\n"
                    + "\n".join(cand_lines)
                    + "\nEspecifique pelo número de /tarefas ou título mais exato."
                )
            if not assignment:
                return f"Tarefa '{arg}' não encontrada."
            if is_resolution_request(arg):
                cname = short_course_name(assignment.get("course_name"))
                return format_refusal_message(assignment.get("name") or "", cname)
            res = await generate_task_briefing(
                repo, study_provider, user, assignment, tz, settings=settings
            )
            return res.text

        async def on_explain(query: str) -> str:
            res = await explain_topic(repo, study_provider, user, query, settings=settings)
            return res.text

        async def on_summary(query: str) -> str:
            parts = query.split(maxsplit=2)
            c_q = parts[0] if parts else ""
            f_q = parts[1] if len(parts) > 1 else ""
            r_q = parts[2] if len(parts) > 2 else None
            res = await summarize_file(repo, study_provider, user, c_q, f_q, r_q, settings=settings)
            return res.text

        async def on_quiz(query: str) -> str:
            parts = query.split()
            n_val = 5
            if parts and parts[-1].isdigit():
                n_val = int(parts.pop())
            c_q = parts[0] if parts else ""
            t_q = " ".join(parts[1:]) if len(parts) > 1 else c_q
            _, res = await generate_quiz(
                repo, study_provider, user, c_q, t_q, n=n_val, settings=settings
            )
            return res.text

        async def on_answer_key() -> str:
            return get_quiz_answer_key(user.user_id)

        async def on_usage() -> str:
            return get_usage_report(repo, user.user_id)

        async def on_status() -> str:
            return build_status_text(repo, settings)

        async def on_health() -> str:
            return build_health_text(
                repo,
                settings,
                incidents=incidents,
                started_at=started_at,
            )

        from .knowledge.review_service import ReviewService

        review_service = ReviewService(repo, user, tz, settings)

        async def on_review(arg: str) -> str:
            if arg.strip().lower() == "cancelar":
                if review_service.cancel_session():
                    return "Sessão de revisão encerrada."
                return "Não há sessão de revisão ativa."
            assignment, candidates, err = review_service.resolve_task(arg)
            if err:
                return err
            if candidates:
                cand_lines = [
                    f"- <b>{c.get('name')}</b> ({short_course_name(c.get('course_name'))})"
                    for c in candidates
                ]
                return (
                    f"Mais de uma tarefa corresponde a '{arg}':\n"
                    + "\n".join(cand_lines)
                    + "\nEspecifique pelo número de /tarefas ou título mais exato."
                )
            if not assignment:
                return f"Tarefa '{arg}' não encontrada."
            if review_service.is_quiz(assignment):
                return "Esta tarefa é um quiz. A revisão de rascunho não se aplica a quizzes."
            session = review_service.start_session(assignment)
            return review_service.build_catchup_message_intro(session)

        async def on_review_document(update, context) -> str:
            session = review_service.get_session()
            if session is None:
                return "Não há sessão de revisão ativa. Use /revisar <n | título> para iniciar."
            doc = update.effective_message.document if update.effective_message else None
            if doc is None:
                return "Nenhum documento recebido."
            file_obj = await doc.get_file()
            content = await file_obj.download_as_bytearray()
            import tempfile
            from pathlib import Path

            suffix = ""
            if doc.file_name:
                from pathlib import PurePosixPath

                suffix = PurePosixPath(doc.file_name).suffix
            temp_path = Path(tempfile.mkstemp(suffix=suffix)[1])
            temp_path.write_bytes(content)
            err = review_service.validate_file(temp_path, settings.review_max_file_mb)
            if err:
                temp_path.unlink(missing_ok=True)
                return err
            draft_text = review_service.extract_text_from_file(temp_path)
            if not draft_text.strip():
                return "Não consegui extrair texto do arquivo. Verifique se ele não está vazio."
            result = await review_service.review_draft(study_provider, session, draft_text)
            return result.text

        # Handler para texto livre durante sessão de revisão
        _original_on_message = on_message

        async def on_message_with_review(_user_obj: object, text: str) -> str:
            session = review_service.get_session()
            if (
                session is not None
                and text.strip().lower() not in ("/revisar", "/cancelar")
                and not text.startswith("/")
                and len(text) <= settings.review_max_text_chars
            ):
                result = await review_service.review_draft(study_provider, session, text)
                return result.text
            return await _original_on_message(_user_obj, text)

        channel = TelegramChannel(
            settings.telegram_bot_token.get_secret_value(),
            user=user,
            chat_id=user.telegram_chat_id,
            allowed_user_ids=user.telegram_user_ids,
            on_message=on_message_with_review,
            on_sync=on_sync,
            on_status=on_status,
            on_health=on_health,
            on_files=on_files,
            on_search=on_search,
            on_snippet=on_snippet,
            on_tasks=on_tasks,
            on_task_briefing=on_task_briefing,
            on_explain=on_explain,
            on_summary=on_summary,
            on_quiz=on_quiz,
            on_answer_key=on_answer_key,
            on_usage=on_usage,
            on_test_notify=notifications.send_test if notifications else None,
            on_review=on_review,
            on_review_document=on_review_document,
            handler_timeout=settings.llm_timeout_seconds + 60,
        )
        incidents.notifier = channel
        notifications = NotificationService(repo, user, tz, channel, settings.notification)
        channel.on_test_notify = notifications.send_test

        try:
            logger.info("Sincronização inicial…")
            try:
                initial = await runner.run(block=True)
                if initial is not None:
                    await incidents.canvas_success()
                    mark_scheduler_cycle(repo, user.user_id)
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
                await record_canvas_exception(exc)
                repo.add_issue(user.user_id, "sync", str(exc), 0, getattr(exc, "status_code", None))

            # Catch-up ao iniciar: detecta lacuna > CATCHUP_GAP_MINUTES desde o último ciclo
            try:
                run_catchup, last_cycle, now = should_run_catchup(
                    repo, user.user_id, settings.catchup_gap_minutes
                )
                if run_catchup and last_cycle is not None:
                    cycle_iso = last_cycle.isoformat()
                    from .catchup import already_sent_catchup

                    if not already_sent_catchup(repo, user.user_id, cycle_iso):
                        logger.info(
                            "Catch-up: lacuna de %s desde %s; enviando mensagem consolidada",
                            now - last_cycle,
                            last_cycle.isoformat(),
                        )
                        msg = build_catchup_message(
                            repo,
                            user,
                            settings,
                            tz,
                            last_cycle=last_cycle,
                            now=now,
                        )
                        sent = await channel.send_message(user, msg)
                        if sent:
                            mark_catchup_sent(repo, user.user_id, cycle_iso)
            except Exception:
                logger.exception("Falha ao executar catch-up ao iniciar")

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
            if settings.content_sync_enabled:
                scheduler.add_job(
                    content_sync_job,
                    "interval",
                    minutes=settings.content_sync_interval_minutes,
                    id="content_sync",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=300,
                    next_run_time=datetime.now(tz) + timedelta(seconds=30),
                )
            if settings.heartbeat_url:
                scheduler.add_job(
                    heartbeat_job,
                    "interval",
                    minutes=settings.heartbeat_interval_minutes,
                    id="heartbeat",
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=60,
                )
                logger.info(
                    "Heartbeat externo a cada %s min para %s",
                    settings.heartbeat_interval_minutes,
                    settings.heartbeat_url,
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
