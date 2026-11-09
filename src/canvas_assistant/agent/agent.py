from __future__ import annotations

import asyncio
import logging
from collections import deque
from zoneinfo import ZoneInfo

from ..config import UserProfile
from ..llm.base import LLMProvider, Message
from ..repository import Repository
from ..timeutil import local_now
from .prompts import build_system_prompt
from .tools import ToolRegistry

logger = logging.getLogger(__name__)

FALLBACK_ANSWER = "Não consegui concluir a consulta agora. Tente de novo ou use /sync."


class Agent:
    def __init__(
        self,
        provider: LLMProvider,
        repo: Repository,
        user: UserProfile,
        tz: ZoneInfo,
        *,
        max_tool_rounds: int = 5,
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
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self) -> asyncio.Lock:
        lock = self._locks.get(self.user.user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[self.user.user_id] = lock
        return lock

    def _system_message(self) -> Message:
        courses = self.repo.get_courses(
            self.user.user_id, current_only=True, include_non_subjects=False
        )
        prompt = build_system_prompt(local_now(self.tz), self.tz.key, courses)
        return Message(role="system", content=prompt)

    def _remember(self, user_text: str, answer: str) -> None:
        history = self._history.setdefault(self.user.user_id, deque(maxlen=self._history_turns))
        history.append((user_text, answer))

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

            messages = [
                self._system_message(),
                *self._previous_messages(),
                Message(role="user", content=text),
            ]

            try:
                for _ in range(self.max_tool_rounds):
                    response = await self.provider.chat(messages, self.tools.specs)
                    if not response.tool_calls:
                        answer = (response.text or "").strip()
                        if not answer:
                            answer = (
                                "Não consegui montar uma resposta. Tente reformular a pergunta."
                            )
                        self._remember(text, answer)
                        return answer

                    messages.append(
                        Message(
                            role="assistant",
                            content=response.text or "",
                            tool_calls=response.tool_calls,
                        )
                    )
                    for call in response.tool_calls:
                        result = await self.tools.execute(call.name, call.arguments)
                        messages.append(
                            Message(
                                role="tool",
                                content=result,
                                tool_call_id=call.id,
                                tool_name=call.name,
                            )
                        )
            except Exception:
                logger.exception("Falha ao consultar o LLM ou executar ferramentas")

            self._remember(text, FALLBACK_ANSWER)
            return FALLBACK_ANSWER

    async def aclose(self) -> None:
        await self.provider.aclose()
