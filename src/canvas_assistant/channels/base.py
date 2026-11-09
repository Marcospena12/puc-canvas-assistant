from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from ..config import UserProfile

MessageHandler = Callable[[UserProfile, str], Awaitable[str | None]]


class Channel(Protocol):
    async def send_message(self, user: UserProfile, text: str) -> bool: ...

    async def start(self, handler: MessageHandler) -> None: ...

    async def stop(self) -> None: ...
