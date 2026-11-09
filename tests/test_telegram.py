from __future__ import annotations

from types import SimpleNamespace

import pytest

from canvas_assistant.channels.telegram import TelegramChannel, split_text
from canvas_assistant.config import UserProfile


async def _dummy_message(*args, **kwargs) -> str:
    return "ok"


async def _dummy_sync() -> str:
    return "ok"


def make_channel(**overrides) -> TelegramChannel:
    data = {
        "token": "token",
        "user": UserProfile(
            user_id="test",
            canvas_base_url="https://canvas.test",
            canvas_token="token",
            telegram_chat_id=10,
            telegram_user_ids=frozenset({20}),
        ),
        "chat_id": 10,
        "allowed_user_ids": frozenset({20}),
        "on_message": _dummy_message,
        "on_sync": _dummy_sync,
        "on_status": _dummy_sync,
    }
    data.update(overrides)
    return TelegramChannel(**data)


def make_update(user_id: int, chat_id: int) -> SimpleNamespace:
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(id=chat_id),
    )


def test_split_text_respects_limit_and_newlines() -> None:
    chunks = split_text("a" * 9000)
    assert [len(chunk) for chunk in chunks] == [4000, 4000, 1000]

    chunks = split_text("x" * 50 + "\n" + "y" * 40, limit=60)
    assert chunks == ["x" * 50, "y" * 40]


def test_split_text_empty() -> None:
    assert split_text("   ") == []


def test_authorized_requires_allowlist_match() -> None:
    channel = make_channel()

    assert channel._authorized(make_update(20, 10)) is True
    assert channel._authorized(make_update(21, 10)) is False


def test_authorized_requires_configured_chat() -> None:
    channel = make_channel()

    assert channel._authorized(make_update(20, 11)) is False


def test_authorized_accepts_chat_only_config() -> None:
    channel = make_channel(allowed_user_ids=frozenset())

    assert channel._authorized(make_update(99, 10)) is True


def test_channel_requires_some_authorization() -> None:
    with pytest.raises(ValueError):
        make_channel(chat_id=None, allowed_user_ids=frozenset())


async def test_on_text_passes_user_to_handler_and_replies() -> None:
    calls: list[tuple[UserProfile, str]] = []
    replies: list[str] = []

    async def handler(user: UserProfile, text: str) -> str:
        calls.append((user, text))
        return "resposta"

    async def reply_text(text: str, **kwargs) -> None:
        replies.append(text)

    async def send_action(action: str) -> None:
        return None

    channel = make_channel(on_message=handler)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=20),
        effective_chat=SimpleNamespace(id=10, send_action=send_action),
        effective_message=SimpleNamespace(text="oi", reply_text=reply_text),
    )

    await channel._on_text(update, None)

    assert calls == [(channel.user, "oi")]
    assert replies == ["resposta"]
