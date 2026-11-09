from __future__ import annotations

import asyncio
import logging
from collections import deque
from zoneinfo import ZoneInfo

from ..config import UserProfile
from ..llm.base import LLMProvider, Message
from ..repository import Repository
from ..timeutil import local_now
from .context import Evidence, build_context_pack
from .grounding import (
    ToolObservation,
    build_evidence,
    correction_message,
    deterministic_fallback,
    parse_tool_result,
    validate_answer,
)
from .prompts import build_system_prompt
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

FALLBACK_ANSWER = "Não consegui concluir a consulta agora. Tente de novo ou use /sync."
UNCONFIRMED_ANSWER = (
    "Não consegui confirmar os dados dessa resposta. Tente perguntar de novo ou use /sync."
)


class Agent:
    def __init__(
        self,
        provider: LLMProvider,
        repo: Repository,
        user: UserProfile,
        tz: ZoneInfo,
        *,
        max_tool_rounds: int = 6,
        history_turns: int = 6,
    ) -> None:
        self.provider = provider
        self.repo = repo
        self.user = user
        self.tz = tz
        self.max_tool_rounds = max_tool_rounds
        self.tools = ToolRegistry(repo, user, tz)
        self._history: dict[str, deque[tuple[str, str]]] = {}
        self._history_turns = history_turns
        self._last_evidence: dict[str, Evidence] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self) -> asyncio.Lock:
        lock = self._locks.get(self.user.user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[self.user.user_id] = lock
        return lock

    def _system_message(self) -> tuple[Message, frozenset[str]]:
        now = local_now(self.tz)
        courses = self.repo.get_courses(
            self.user.user_id, current_only=True, include_non_subjects=False
        )
        context = build_context_pack(self.repo, self.user, self.tz, now)
        evidence = self._last_evidence.get(self.user.user_id)
        prompt = build_system_prompt(now, self.tz.key, courses, context, evidence)
        allowed = set(context.urls)
        if evidence:
            allowed.update(evidence.urls)
        return Message(role="system", content=prompt), frozenset(allowed)

    def _remember(self, user_text: str, answer: str) -> None:
        history = self._history.setdefault(self.user.user_id, deque(maxlen=self._history_turns))
        history.append((user_text, answer))

    def _finalize(
        self,
        user_text: str,
        answer: str,
        observations: list[ToolObservation],
    ) -> str:
        if observations:
            self._last_evidence[self.user.user_id] = build_evidence(observations)
        self._remember(user_text, answer)
        return answer

    def _previous_messages(self) -> list[Message]:
        history = self._history.get(self.user.user_id)
        if not history:
            return []
        messages: list[Message] = []
        for user_text, answer in history:
            messages.append(Message(role="user", content=user_text))
            messages.append(Message(role="assistant", content=answer))
        return messages

    async def reply(self, text: str) -> str:
        async with self._lock():
            if self.repo.is_empty(self.user.user_id):
                return "Ainda não tenho dados do Canvas. Rode /sync e tente novamente."

            system, grounded_urls = self._system_message()
            messages = [
                system,
                *self._previous_messages(),
                Message(role="user", content=text),
            ]
            allowed_urls = set(grounded_urls)
            observations: list[ToolObservation] = []
            correction_sent = False

            try:
                for _ in range(self.max_tool_rounds):
                    response = await self.provider.chat(messages, self.tools.specs)
                    if response.tool_calls:
                        messages.append(
                            Message(
                                role="assistant",
                                content=response.text or "",
                                tool_calls=response.tool_calls,
                            )
                        )
                        for call in response.tool_calls:
                            result = await self.tools.execute(call.name, call.arguments)
                            observation = parse_tool_result(call.name, call.arguments, result)
                            observations.append(observation)
                            allowed_urls.update(observation.urls)
                            ok = bool(observation.data and observation.data.get("ok"))
                            logger.info("Ferramenta %s: %s", call.name, "ok" if ok else "erro")
                            messages.append(
                                Message(
                                    role="tool",
                                    content=result,
                                    tool_call_id=call.id,
                                    tool_name=call.name,
                                )
                            )
                        continue

                    answer = (response.text or "").strip()
                    if not answer:
                        fallback = deterministic_fallback(observations)
                        if fallback:
                            return self._finalize(text, fallback, observations)
                        answer = "Não consegui montar uma resposta. Tente reformular a pergunta."
                        return self._finalize(text, answer, observations)

                    unknown = validate_answer(answer, allowed_urls)
                    if not unknown:
                        return self._finalize(text, answer, observations)

                    logger.warning(
                        "Resposta com dados não confirmados (%s); solicitando correção",
                        ", ".join(sorted(unknown)[:3]),
                    )
                    if correction_sent:
                        fallback = deterministic_fallback(observations)
                        if fallback:
                            return self._finalize(text, fallback, observations)
                        return self._finalize(text, UNCONFIRMED_ANSWER, observations)

                    correction_sent = True
                    messages.append(Message(role="assistant", content=answer))
                    messages.append(Message(role="system", content=correction_message(unknown)))
            except Exception:
                logger.exception("Falha ao consultar o LLM ou executar ferramentas")
                fallback = deterministic_fallback(observations)
                if fallback:
                    return self._finalize(text, fallback, observations)

            self._remember(text, FALLBACK_ANSWER)
            return FALLBACK_ANSWER

    async def aclose(self) -> None:
        await self.provider.aclose()
