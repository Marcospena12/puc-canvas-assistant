from __future__ import annotations

import argparse
import asyncio

import httpx

from .agent import Agent
from .app import SyncRunner, build_status_text, format_sync_result, run_bot
from .canvas import CanvasClient, CanvasService
from .config import Settings
from .db import Database
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
    elif args.command == "ask":
        question = " ".join(args.question)
        asyncio.run(_ask(settings, question))
    elif args.command == "status":
        _status(settings)
    else:
        parser.print_help()
