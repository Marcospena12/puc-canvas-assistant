from __future__ import annotations

from datetime import UTC, datetime, timedelta

from canvas_assistant.config import IncidentConfig
from canvas_assistant.incidents import IncidentService


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, _user, text: str) -> bool:
        self.messages.append(text)
        return True


async def test_canvas_incident_waits_threshold_and_alerts_once(repo, user) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    notifier = FakeNotifier()
    service = IncidentService(
        repo,
        user,
        notifier,
        IncidentConfig(canvas_down_minutes=10),
        clock=lambda: now,
    )

    assert await service.canvas_failure() is False
    now += timedelta(minutes=9)
    assert await service.canvas_failure() is False
    now += timedelta(minutes=1)
    assert await service.canvas_failure() is True
    assert len(notifier.messages) == 1
    assert "10 min" in notifier.messages[0]

    now += timedelta(minutes=5)
    assert await service.canvas_failure() is False
    assert len(notifier.messages) == 1

    assert await service.canvas_success() is True
    assert len(notifier.messages) == 2
    assert "voltou a responder" in notifier.messages[1]
    assert await service.canvas_success() is False


async def test_canvas_auth_incident_is_immediate(repo, user) -> None:
    notifier = FakeNotifier()
    service = IncidentService(
        repo, user, notifier, IncidentConfig(), clock=lambda: datetime.now(UTC)
    )

    assert await service.canvas_auth_failure() is True
    assert await service.canvas_auth_failure() is False
    assert len(notifier.messages) == 1
    assert "CANVAS_TOKEN" in notifier.messages[0]


async def test_llm_incident_alerts_after_three_failures_and_recovers(repo, user) -> None:
    notifier = FakeNotifier()
    service = IncidentService(
        repo,
        user,
        notifier,
        IncidentConfig(llm_failures=3),
        clock=lambda: datetime.now(UTC),
    )

    assert await service.llm_failure() is False
    assert await service.llm_failure() is False
    assert await service.llm_failure() is True
    assert len(notifier.messages) == 1

    assert await service.llm_success() is True
    assert len(notifier.messages) == 2
    assert service.status()["llm_failures"] == 0
