from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx

from .agent import Agent
from .app import SyncRunner, build_status_text, format_sync_result, run_bot
from .backup import check_backup, create_backup, latest_backup
from .canvas import CanvasClient, CanvasService
from .config import Settings
from .db import Database
from .knowledge.content_sync import ContentSyncService
from .knowledge.downloader import FileDownloader
from .knowledge.extraction import ExtractionPipeline
from .knowledge.file_sync import FileSyncService
from .knowledge.report import (
    build_files_text,
    format_file_sync_result,
    format_search_results,
    resolve_course,
)
from .knowledge.search import search
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
from .logging_setup import setup_logging
from .repository import Repository
from .sync import SyncService
from .textutil import short_course_name
from .timeutil import get_timezone


def _make_repo(settings: Settings) -> Repository:
    database = Database(settings.db_path)
    database.initialize()
    return Repository(database)


async def _check(settings: Settings) -> None:
    user = settings.user
    repo = _make_repo(settings)
    print(f"Banco: OK ({settings.db_path})")
    print(f"Cache: {repo.cache_stats(user.user_id)['courses']['count']} curso(s)")

    async with httpx.AsyncClient() as http:
        canvas = CanvasService(CanvasClient(http, user.canvas_base_url, user.canvas_token))
        try:
            await canvas.get_profile()
            print("Canvas: OK (token válido)")
        except Exception as exc:
            print(f"Canvas: FALHA ({exc})")

        if settings.llm_provider == "ollama":
            base = settings.llm.base_url.rstrip("/")
            try:
                response = await http.get(f"{base}/api/tags", timeout=10)
                response.raise_for_status()
                names = [m.get("name", "") for m in response.json().get("models", [])]
                model = settings.llm_model
                found = model in names or any(
                    name.split(":")[0] == model.split(":")[0] for name in names
                )
                if found:
                    print(f"Ollama: OK ({model} disponível em {base})")
                else:
                    print(f"Ollama: modelo '{model}' não encontrado em {base}")
            except Exception as exc:
                print(f"Ollama: FALHA ({exc})")
        else:
            key_state = "configurada" if settings.llm.api_key else "AUSENTE"
            print(f"LLM: {settings.llm_provider}/{settings.llm_model}; API key {key_state}")

    if settings.telegram_bot_token is None:
        print("Telegram: token não configurado (o bot não vai iniciar)")
    elif user.telegram_chat_id is None and not user.telegram_user_ids:
        print("Telegram: sem chat/usuários autorizados (o bot não vai iniciar)")
    else:
        print("Telegram: configurado")


async def _sync(settings: Settings) -> int:
    user = settings.user
    repo = _make_repo(settings)
    tz = get_timezone(settings.timezone)
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
        result = await SyncRunner(sync).run(block=True)
    if result is None:
        print("Sincronização não executada.")
        return 0
    print(format_sync_result(result))
    return 0


async def _sync_files(settings: Settings) -> int:
    user = settings.user
    repo = _make_repo(settings)
    tz = get_timezone(settings.timezone)
    repo.clear_file_retries(user.user_id)
    async with httpx.AsyncClient() as http:
        canvas = CanvasService(CanvasClient(http, user.canvas_base_url, user.canvas_token))
        downloader = FileDownloader(
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
            downloader,
        )
        result = await file_sync.sync_all(download=True)
    print(format_file_sync_result(result))
    return 0


def _files(settings: Settings, query: str) -> None:
    user = settings.user
    repo = _make_repo(settings)
    tz = get_timezone(settings.timezone)
    print(build_files_text(repo, user, tz, query))


def _extract(settings: Settings, *, force_all: bool = False) -> None:
    user = settings.user
    repo = _make_repo(settings)
    pipeline = ExtractionPipeline(repo, user, settings.files_dir)
    res = pipeline.run(force_all=force_all)
    print("Extração de texto concluída:")
    for line in res.as_lines():
        print(f"- {line}")


def _knowledge_stats(settings: Settings) -> None:
    user = settings.user
    repo = _make_repo(settings)
    stats = repo.knowledge_stats(user.user_id)

    print("Estatísticas da Base de Conhecimento:")
    print("Status de extração dos arquivos:")
    for st, count in sorted(stats["status_counts"].items()):
        print(f"- {st}: {count}")

    print("\nChunks por matéria:")
    for c in stats["courses"]:
        name = c["course_name"]
        files_cnt = c["downloaded_files"]
        chunks_cnt = c["chunks"]
        chars_cnt = c["total_chars"]
        print(f"- {name}: {files_cnt} arquivos baixados, {chunks_cnt} chunks, {chars_cnt} chars")

    print("\nDistribuição de caracteres por chunk:")
    print(
        f"- total chunks: {stats['total_chunks']} | mín: {stats['min_chars']} | "
        f"mediana: {stats['median_chars']} | "
        f"média: {stats['avg_chars']} | máx: {stats['max_chars']}"
    )

    fts = stats.get("fts_consistency")
    if fts:
        print(
            f"- consistência do índice FTS5: chunks={fts['chunks_count']} | fts={fts['fts_count']}"
        )


def _reindex(settings: Settings) -> None:
    repo = _make_repo(settings)
    indexed, total = repo.reindex_fts()
    print(f"Reindexação FTS5 concluída: {indexed} chunks indexados (total na base: {total}).")


def _cli_search(
    settings: Settings,
    query: str,
    course_query: str | None,
    k: int,
    source_type: str | None = None,
) -> None:
    user = settings.user
    repo = _make_repo(settings)
    tz = get_timezone(settings.timezone)

    target_course_id: int | None = None
    if course_query:
        courses = repo.get_courses(user.user_id, current_only=True, include_non_subjects=False)
        if not courses:
            courses = repo.get_courses(user.user_id, current_only=False, include_non_subjects=False)
        matched, candidates = resolve_course(courses, course_query)
        if candidates:
            cand_names = [c.get("name") for c in candidates]
            print(
                f"Mais de uma matéria corresponde a '{course_query}':\n"
                + "\n".join(f"- {n}" for n in cand_names)
            )
            return
        if not matched:
            print(f"Matéria '{course_query}' não encontrada.")
            return
        target_course_id = int(matched["id"])

    s_types = [source_type] if source_type else None
    results = search(
        repo,
        user.user_id,
        query,
        course_id=target_course_id,
        source_types=s_types,
        k=k,
    )
    print(format_search_results(results, query, tz=tz))


async def _sync_content(settings: Settings, *, force_all: bool = False) -> None:
    user = settings.user
    repo = _make_repo(settings)
    tz = get_timezone(settings.timezone)
    if force_all:
        repo.clear_content_retries(user.user_id)
    async with httpx.AsyncClient(timeout=settings.file_download_timeout_seconds) as http:
        client = CanvasClient(http, user.canvas_base_url, user.canvas_token)
        service = CanvasService(client)
        content_sync = ContentSyncService(
            repo,
            service,
            user,
            tz,
            blocked_retry_hours=settings.content_blocked_retry_hours,
        )
        res = await content_sync.sync_all(notify_baseline=False, force=force_all)
        print("Sincronização de conteúdo concluída:")
        print(f"- Páginas ingeridas: {res.pages_ingested}")
        print(f"- Tarefas ingeridas: {res.assignments_ingested}")
        print(f"- Avisos ingeridos: {res.announcements_ingested}")
        print(f"- Ementas ingeridas: {res.syllabus_ingested}")
        print(f"- Quizzes ingeridos: {res.quizzes_ingested}")
        print(f"- Total de chunks criados: {res.chunks_created}")
        if res.errors:
            print("Erros:")
            for err in res.errors:
                print(f"  • {err}")


async def _ask(settings: Settings, question: str) -> None:
    user = settings.user
    repo = _make_repo(settings)
    tz = get_timezone(settings.timezone)

    if repo.is_empty(user.user_id):
        print("Cache vazio; sincronizando antes de perguntar…")
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
            result = await SyncRunner(sync).run(block=True)
            if result:
                print(format_sync_result(result))

    provider = create_provider(settings.llm)
    agent = Agent(provider, repo, user, tz)
    try:
        answer = await agent.reply(question)
        print(answer)
    finally:
        await agent.aclose()


def _status(settings: Settings) -> None:
    repo = _make_repo(settings)
    print(build_status_text(repo, settings))


def _backup(settings: Settings, keep: int) -> int:
    result = create_backup(
        settings.db_path,
        settings.db_path.parent / "backups",
        keep=keep,
    )
    status = "OK" if result.integrity_ok else "FALHOU"
    print(f"Backup: {result.path} ({result.size_bytes} bytes, integrity={status})")
    if result.removed:
        print(f"Backups antigos removidos: {result.removed}")
    return 0 if result.integrity_ok else 1


def _backup_check(settings: Settings, path: str | None) -> int:
    backup_dir = settings.db_path.parent / "backups"
    target = Path(path) if path else latest_backup(backup_dir)
    if target is None:
        print(f"Nenhum backup encontrado em {backup_dir}")
        return 1
    ok = check_backup(target)
    print(f"Backup: {target} | integrity={'ok' if ok else 'FALHOU'}")
    return 0 if ok else 1


async def _cli_study_explain(settings: Settings, topic: str, course: str | None) -> None:
    repo = _make_repo(settings)
    provider = create_study_provider(settings)
    try:
        res = await explain_topic(
            repo, provider, settings.user, topic, course_query=course, settings=settings
        )
        print(res.text)
        print(
            f"\n[Estatísticas: {res.context_blocks} blocos | "
            f"{res.input_tokens} in / {res.output_tokens} out tokens | {res.latency_ms}ms]"
        )
    finally:
        await provider.aclose()


async def _cli_study_summary(
    settings: Settings, course: str, file_query: str, location_range: str | None
) -> None:
    repo = _make_repo(settings)
    provider = create_study_provider(settings)
    try:
        res = await summarize_file(
            repo, provider, settings.user, course, file_query, location_range, settings=settings
        )
        print(res.text)
        print(
            f"\n[Estatísticas: {res.context_blocks} blocos | "
            f"{res.input_tokens} in / {res.output_tokens} out tokens | "
            f"{res.latency_ms}ms | cache={res.from_cache}]"
        )
    finally:
        await provider.aclose()


async def _cli_study_quiz(settings: Settings, course: str, topic: str, n: int) -> None:
    repo = _make_repo(settings)
    provider = create_study_provider(settings)
    try:
        quiz_data, res = await generate_quiz(
            repo, provider, settings.user, course, topic, n=n, settings=settings
        )
        print(res.text)
        if quiz_data:
            print(
                f"\n[Estatísticas: {res.context_blocks} blocos | "
                f"{res.input_tokens} in / {res.output_tokens} out tokens | {res.latency_ms}ms]"
            )
            ans = get_quiz_answer_key(settings.user.user_id)
            print("\n" + "=" * 50)
            print(ans)
    finally:
        await provider.aclose()


def _cli_study_usage(settings: Settings) -> None:
    repo = _make_repo(settings)
    print(get_usage_report(repo, settings.user.user_id))


def _cli_study_tasks(settings: Settings, course: str | None) -> None:
    repo = _make_repo(settings)
    tz = get_timezone(settings.timezone)
    print(list_pending_tasks_text(repo, settings.user.user_id, tz, course_query=course or ""))


async def _cli_study_task(settings: Settings, query: str) -> None:
    repo = _make_repo(settings)
    tz = get_timezone(settings.timezone)
    assignment, candidates, err = resolve_task_selection(repo, settings.user.user_id, query, tz)
    if err:
        print(err)
        return
    if candidates:
        print(f"Mais de uma tarefa corresponde a '{query}':")
        for c in candidates:
            cname = short_course_name(c.get("course_name"))
            print(f"- {c.get('name')} ({cname})")
        print("Especifique pelo número de /tarefas ou título mais exato.")
        return
    if not assignment:
        print(f"Tarefa '{query}' não encontrada.")
        return
    if is_resolution_request(query):
        cname = short_course_name(assignment.get("course_name"))
        print(format_refusal_message(assignment.get("name") or "", cname))
        return

    provider = create_study_provider(settings)
    try:
        res = await generate_task_briefing(
            repo, provider, settings.user, assignment, tz, settings=settings
        )
        print(res.text)
        print(
            f"\n[Estatísticas: {res.context_blocks} blocos | "
            f"{res.input_tokens} in / {res.output_tokens} out tokens | {res.latency_ms}ms"
            f"{' (do cache)' if res.from_cache else ''}]"
        )
    finally:
        await provider.aclose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="canvas-assistant")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="inicia o bot do Telegram")
    sub.add_parser("check", help="valida configuração, Canvas e modelo local")
    sub.add_parser("sync", help="sincroniza o Canvas com o cache local")
    sub.add_parser("sync-files", help="sincroniza e baixa arquivos das matérias ativas")
    sync_content_parser = sub.add_parser(
        "sync-content", help="sincroniza páginas, tarefas, avisos e ementas"
    )
    sync_content_parser.add_argument(
        "--all",
        action="store_true",
        help="reprocessa todo o conteúdo, ignorando hashes e bloqueios temporários",
    )
    files_parser = sub.add_parser("files", help="lista arquivos conhecidos de uma matéria")
    files_parser.add_argument("query", nargs="*", help="nome da matéria ou parte dele (opcional)")
    extract_parser = sub.add_parser("extract", help="extrai texto dos arquivos baixados")
    extract_parser.add_argument(
        "--all",
        action="store_true",
        help="reprocessa todos os arquivos, mesmo os já extraídos",
    )
    sub.add_parser("knowledge-stats", help="estatísticas da base de conhecimento (chunks e status)")
    sub.add_parser("reindex", help="reconstrói o índice de busca FTS5 a partir dos chunks")
    search_parser = sub.add_parser("search", help="pesquisa termos nos materiais de aula")
    search_parser.add_argument("query", help="termo ou frase de busca")
    search_parser.add_argument("--course", help="filtra por matéria (opcional)")
    search_parser.add_argument(
        "--type",
        choices=["arquivo", "pagina", "tarefa", "ementa", "aviso"],
        help="filtra por tipo de conteúdo",
    )
    search_parser.add_argument(
        "-k", type=int, default=8, help="número máximo de resultados (padrão: 8)"
    )
    ask_parser = sub.add_parser("ask", help="faz uma pergunta ao agente pelo terminal")
    ask_parser.add_argument("question", nargs="+", help="pergunta em linguagem natural")
    sub.add_parser("status", help="mostra o estado do cache")
    backup_parser = sub.add_parser("backup", help="cria backup consistente do banco")
    backup_parser.add_argument("--keep", type=int, default=7, help="backups a manter (padrão: 7)")
    backup_check_parser = sub.add_parser("backup-check", help="verifica integridade de um backup")
    backup_check_parser.add_argument("--path", help="arquivo de backup (padrão: mais recente)")

    study_parser = sub.add_parser("study", help="ferramentas de estudo com LLM")
    study_sub = study_parser.add_subparsers(dest="study_command")

    sp_explain = study_sub.add_parser("explain", help="explica um tópico acadêmico com fontes")
    sp_explain.add_argument("topic", nargs="+", help="tópico ou dúvida")
    sp_explain.add_argument("--course", help="filtra por matéria (opcional)")

    sp_summary = study_sub.add_parser("summary", help="resumo estruturado de um arquivo de aula")
    sp_summary.add_argument("course", help="nome ou código da matéria")
    sp_summary.add_argument("file", help="nome ou trecho do arquivo")
    sp_summary.add_argument(
        "range", nargs="?", default=None, help="faixa de páginas/slides (opcional)"
    )

    sp_quiz = study_sub.add_parser("quiz", help="gera quiz formativo sobre um tópico")
    sp_quiz.add_argument("course", help="nome ou código da matéria")
    sp_quiz.add_argument("topic", help="tópico do quiz")
    sp_quiz.add_argument("-n", type=int, default=5, help="número de questões (padrão 5)")

    study_sub.add_parser("usage", help="relatório de consumo de tokens dos últimos 7 dias")

    sp_tasks = study_sub.add_parser("tasks", help="lista tarefas pendentes por prazo")
    sp_tasks.add_argument("--course", help="filtra por matéria (opcional)")

    sp_task = study_sub.add_parser("task", help="briefing estruturado de uma tarefa")
    sp_task.add_argument("query", nargs="+", help="número de /tarefas ou título da tarefa")

    args = parser.parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level, log_file=Path("data/logs/assistant.log"))

    if args.command in (None, "run"):
        try:
            asyncio.run(run_bot(settings))
        except KeyboardInterrupt:
            print("\nEncerrado.")
    elif args.command == "check":
        asyncio.run(_check(settings))
    elif args.command == "sync":
        try:
            asyncio.run(_sync(settings))
        except Exception as exc:
            print(f"Falha na sincronização: {exc}")
            raise SystemExit(1) from exc
    elif args.command == "sync-files":
        try:
            asyncio.run(_sync_files(settings))
        except Exception as exc:
            print(f"Falha na sincronização de arquivos: {exc}")
            raise SystemExit(1) from exc
    elif args.command == "sync-content":
        try:
            asyncio.run(_sync_content(settings, force_all=bool(args.all)))
        except Exception as exc:
            print(f"Falha na sincronização de conteúdo: {exc}")
            raise SystemExit(1) from exc
    elif args.command == "files":
        query = " ".join(args.query) if args.query else ""
        _files(settings, query)
    elif args.command == "extract":
        _extract(settings, force_all=bool(args.all))
    elif args.command == "knowledge-stats":
        _knowledge_stats(settings)
    elif args.command == "reindex":
        _reindex(settings)
    elif args.command == "search":
        _cli_search(settings, args.query, args.course, args.k, getattr(args, "type", None))
    elif args.command == "ask":
        question = " ".join(args.question)
        asyncio.run(_ask(settings, question))
    elif args.command == "status":
        _status(settings)
    elif args.command == "backup":
        raise SystemExit(_backup(settings, max(1, args.keep)))
    elif args.command == "backup-check":
        raise SystemExit(_backup_check(settings, args.path))
    elif args.command == "study":
        if args.study_command == "explain":
            topic_str = " ".join(args.topic)
            asyncio.run(_cli_study_explain(settings, topic_str, args.course))
        elif args.study_command == "summary":
            asyncio.run(_cli_study_summary(settings, args.course, args.file, args.range))
        elif args.study_command == "quiz":
            asyncio.run(_cli_study_quiz(settings, args.course, args.topic, args.n))
        elif args.study_command == "usage":
            _cli_study_usage(settings)
        elif args.study_command == "tasks":
            _cli_study_tasks(settings, args.course)
        elif args.study_command == "task":
            query_str = " ".join(args.query)
            asyncio.run(_cli_study_task(settings, query_str))
        else:
            study_parser.print_help()
    else:
        parser.print_help()
