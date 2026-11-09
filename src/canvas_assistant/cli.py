from __future__ import annotations

import argparse
import asyncio

import httpx

from .agent import Agent
from .app import SyncRunner, build_status_text, format_sync_result, run_bot
from .canvas import CanvasClient, CanvasService
from .config import Settings
from .db import Database
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
from .llm import create_provider
from .logging_setup import setup_logging
from .repository import Repository
from .sync import SyncService
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


def _cli_search(settings: Settings, query: str, course_query: str | None, k: int) -> None:
    user = settings.user
    repo = _make_repo(settings)

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

    results = search(repo, user.user_id, query, course_id=target_course_id, k=k)
    print(format_search_results(results, query))


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="canvas-assistant")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="inicia o bot do Telegram")
    sub.add_parser("check", help="valida configuração, Canvas e modelo local")
    sub.add_parser("sync", help="sincroniza o Canvas com o cache local")
    sub.add_parser("sync-files", help="sincroniza e baixa arquivos das matérias ativas")
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
        "-k", type=int, default=8, help="número máximo de resultados (padrão: 8)"
    )
    ask_parser = sub.add_parser("ask", help="faz uma pergunta ao agente pelo terminal")
    ask_parser.add_argument("question", nargs="+", help="pergunta em linguagem natural")
    sub.add_parser("status", help="mostra o estado do cache")

    args = parser.parse_args(argv)
    settings = Settings()
    setup_logging(settings.log_level)

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
        _cli_search(settings, args.query, args.course, args.k)
    elif args.command == "ask":
        question = " ".join(args.question)
        asyncio.run(_ask(settings, question))
    elif args.command == "status":
        _status(settings)
    else:
        parser.print_help()
