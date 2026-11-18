from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from .config import IncidentConfig, UserProfile
from .repository import Repository
from .timeutil import parse_dt, to_utc_iso, utc_now

logger = logging.getLogger(__name__)


class IncidentNotifier(Protocol):
    async def send_message(self, user: UserProfile, text: str) -> bool: ...


class IncidentService:
    """Alerta incidentes uma vez e avisa quando o serviço se recupera."""

    def __init__(
        self,
        repo: Repository,
        user: UserProfile,
        notifier: IncidentNotifier | None,
        config: IncidentConfig,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repo = repo
        self.user = user
        self.notifier = notifier
        self.config = config
        self.clock = clock

    def _key(self, suffix: str) -> str:
        return f"incident:{suffix}:{self.user.user_id}"

    def _get_dt(self, suffix: str) -> datetime | None:
        return parse_dt(self.repo.get_meta(self._key(suffix)))

    def _set_dt(self, suffix: str, value: datetime) -> None:
        self.repo.set_meta(self._key(suffix), to_utc_iso(value))

    def _is_active(self, suffix: str) -> bool:
        return self.repo.get_meta(self._key(suffix)) == "1"

    def _set_active(self, suffix: str, active: bool) -> None:
        self.repo.set_meta(self._key(suffix), "1" if active else "0")

    async def _send(self, text: str) -> bool:
        if self.notifier is None:
            return False
        try:
            return await self.notifier.send_message(self.user, text)
        except Exception:
            logger.exception("Falha ao enviar alerta de incidente")
            return False

    async def _activate(self, suffix: str, text: str, *, delay: timedelta) -> bool:
        if not self.config.enabled or self._is_active(suffix):
            return False
        now = self.clock()
        first = self._get_dt(f"{suffix}:first")
        if first is None:
            self._set_dt(f"{suffix}:first", now)
            first = now
        if now - first < delay:
            return False
        if await self._send(text):
            self._set_active(suffix, True)
            self._set_dt(f"{suffix}:last_alert", now)
            logger.warning("Incidente ativado: %s", suffix)
            return True
        return False

    async def _recover(self, suffix: str, text: str) -> bool:
        if not self._is_active(suffix):
            self._set_active(suffix, False)
            return False
        if await self._send(text):
            self._set_active(suffix, False)
            self.repo.set_meta(self._key(f"{suffix}:first"), "")
            logger.info("Incidente recuperado: %s", suffix)
            return True
        return False

    async def canvas_auth_failure(self) -> bool:
        return await self._activate(
            "canvas_auth",
            "Canvas recusou o token (401/403). Verifique CANVAS_TOKEN no .env.",
            delay=timedelta(0),
        )

    async def canvas_failure(self) -> bool:
        minutes = self.config.canvas_down_minutes
        return await self._activate(
            "canvas",
            f"Canvas indisponível há mais de {minutes} min. "
            "As sincronizações serão tentadas novamente.",
            delay=timedelta(minutes=minutes),
        )

    async def canvas_success(self) -> bool:
        recovered = await self._recover(
            "canvas",
            "Canvas voltou a responder. Sincronizações normalizadas.",
        )
        auth_recovered = await self._recover(
            "canvas_auth",
            "Autenticação do Canvas normalizada.",
        )
        return recovered or auth_recovered

    async def llm_failure(self) -> bool:
        if not self.config.enabled or self._is_active("llm"):
            return False
        count = int(self.repo.get_meta(self._key("llm:failures")) or "0") + 1
        self.repo.set_meta(self._key("llm:failures"), str(count))
        if count < self.config.llm_failures:
            return False
        if await self._send(
            f"O modelo de linguagem falhou {count} vezes seguidas. "
            "Verifique o Ollama/LLM e os logs."
        ):
            self._set_active("llm", True)
            self._set_dt("llm:last_alert", self.clock())
            logger.warning("Incidente ativado: llm")
            return True
        return False

    async def llm_success(self) -> bool:
        self.repo.set_meta(self._key("llm:failures"), "0")
        return await self._recover("llm", "O modelo de linguagem voltou a responder.")

    def status(self) -> dict[str, bool | int]:
        return {
            "canvas_active": self._is_active("canvas"),
            "canvas_auth_active": self._is_active("canvas_auth"),
            "llm_active": self._is_active("llm"),
            "llm_failures": int(self.repo.get_meta(self._key("llm:failures")) or "0"),
        }


class IncidentAwareProvider:
    """Envolve um LLMProvider para contar falhas sem alterar o agente."""

    def __init__(self, provider: object, incidents: IncidentService) -> None:
        self.provider = provider
        self.incidents = incidents

    async def chat(self, messages: object, tools: object) -> object:
        try:
            response = await self.provider.chat(messages, tools)  # type: ignore[attr-defined]
        except Exception:
            await self.incidents.llm_failure()
            raise
        await self.incidents.llm_success()
        return response

    async def aclose(self) -> None:
        await self.provider.aclose()  # type: ignore[attr-defined]
