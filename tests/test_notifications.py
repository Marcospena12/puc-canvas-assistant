from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from canvas_assistant.config import NotificationConfig
from canvas_assistant.models import Announcement
from canvas_assistant.notifications import NotificationService
from canvas_assistant.timeutil import to_utc_iso

NOW = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)


def make_config(**overrides) -> NotificationConfig:
    values = {
        "enabled": True,
        "new_assignments": True,
        "due_hours": (24, 2),
        "announcements": True,
        "digest": True,
        "digest_time": time(8, 0),
        "quiet_start": time(22, 0),
        "quiet_end": time(7, 0),
        "max_stale_minutes": 45,
        "digest_grace_minutes": 120,
    }
    values.update(overrides)
    return NotificationConfig(**values)


class FakeChannel:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.messages: list[str] = []
        self.attempts = 0

    async def send_message(self, user, text: str) -> bool:
        self.attempts += 1
        if self.ok:
            self.messages.append(text)
        return self.ok


def make_service(repo, user, tz, channel: FakeChannel, **overrides) -> NotificationService:
    return NotificationService(repo, user, tz, channel, make_config(**overrides))


def seed_course(repo, user, course) -> None:
    repo.upsert_courses(user.user_id, [course])


def seed_task(repo, user, course, assignment) -> None:
    seed_course(repo, user, course)
    repo.replace_assignments(user.user_id, course.id, [assignment])


def far_due() -> str:
    return to_utc_iso(NOW + timedelta(days=10))


async def test_baseline_suppresses_existing_and_sends_only_new(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    course = course_factory()
    first = assignment_factory(id=100, name="Lista 1", due_at=far_due())
    seed_task(repo, user, course, first)
    channel = FakeChannel()
    service = make_service(repo, user, tz, channel)
    service.mark_sync_success(NOW)

    run = await service.run_cycle(NOW)

    assert run.baseline is True
    assert channel.messages == []

    second = assignment_factory(id=101, name="Nova tarefa", due_at=far_due())
    repo.replace_assignments(user.user_id, course.id, [first, second])

    run = await service.run_cycle(NOW + timedelta(minutes=1))

    assert run.sent == 1
    assert len(channel.messages) == 1
    assert "Nova tarefa" in channel.messages[0]
    assert "Lista 1" not in channel.messages[0]

    run = await service.run_cycle(NOW + timedelta(minutes=2))
    assert run.sent == 0
    assert len(channel.messages) == 1


async def test_failed_send_is_retried(repo, user, tz, course_factory, assignment_factory) -> None:
    course = course_factory()
    first = assignment_factory(id=100, name="Lista 1", due_at=far_due())
    seed_task(repo, user, course, first)
    channel = FakeChannel(ok=False)
    service = make_service(repo, user, tz, channel)
    service.mark_sync_success(NOW)
    await service.run_cycle(NOW)

    second = assignment_factory(id=101, name="Nova tarefa", due_at=far_due())
    repo.replace_assignments(user.user_id, course.id, [first, second])

    run = await service.run_cycle(NOW + timedelta(minutes=1))
    assert run.sent == 0
    assert channel.attempts == 1
    assert channel.messages == []

    channel.ok = True
    run = await service.run_cycle(NOW + timedelta(minutes=2))
    assert run.sent == 1
    assert channel.attempts == 2
    assert "Nova tarefa" in channel.messages[0]


async def test_due_reminders_use_thresholds_without_duplicates(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    course = course_factory()
    assignment = assignment_factory(
        id=100,
        name="Prova",
        due_at=to_utc_iso(NOW + timedelta(hours=20)),
    )
    seed_task(repo, user, course, assignment)
    channel = FakeChannel()
    service = make_service(repo, user, tz, channel)
    service.mark_sync_success(NOW)
    await service.run_cycle(NOW)

    run = await service.run_cycle(NOW + timedelta(minutes=1))
    assert run.sent == 1
    assert "Prazos próximos" in channel.messages[0]

    updated = assignment_factory(
        id=100,
        name="Prova",
        due_at=to_utc_iso(NOW + timedelta(hours=1)),
    )
    repo.replace_assignments(user.user_id, course.id, [updated])

    run = await service.run_cycle(NOW + timedelta(minutes=2))
    assert run.sent == 1
    assert len(channel.messages) == 2

    run = await service.run_cycle(NOW + timedelta(minutes=3))
    assert run.sent == 0
    assert len(channel.messages) == 2


async def test_quiet_hours_defer_notifications(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    course = course_factory()
    first = assignment_factory(id=100, name="Lista 1", due_at=far_due())
    seed_task(repo, user, course, first)
    channel = FakeChannel()
    service = make_service(repo, user, tz, channel)
    service.mark_sync_success(NOW)
    await service.run_cycle(NOW)

    second = assignment_factory(id=101, name="Nova tarefa", due_at=far_due())
    repo.replace_assignments(user.user_id, course.id, [first, second])

    quiet_now = datetime(2026, 11, 10, 2, 0, tzinfo=UTC)
    service.mark_sync_success(quiet_now)
    run = await service.run_cycle(quiet_now)
    assert run.reason == "quiet_hours"
    assert channel.messages == []

    after_quiet = datetime(2026, 11, 10, 10, 1, tzinfo=UTC)
    service.mark_sync_success(after_quiet)
    run = await service.run_cycle(after_quiet)
    assert run.sent == 1
    assert "Nova tarefa" in channel.messages[0]


async def test_new_announcement_is_sent_once(repo, user, tz, course_factory) -> None:
    course = course_factory()
    seed_course(repo, user, course)
    channel = FakeChannel()
    service = make_service(repo, user, tz, channel)
    service.mark_sync_success(NOW)
    await service.run_cycle(NOW)

    repo.upsert_announcements(
        user.user_id,
        [
            Announcement(
                id=500,
                course_id=course.id,
                title="Mudança de sala",
                message_html="<p>A aula será no laboratório.</p>",
                author="Prof. Teste",
                posted_at=to_utc_iso(NOW - timedelta(hours=1)),
                url="https://canvas.test/courses/1/announcements/500",
                read_state="unread",
            )
        ],
    )

    run = await service.run_cycle(NOW + timedelta(minutes=1))
    assert run.sent == 1
    assert "Novos avisos" in channel.messages[0]
    assert "Mudança de sala" in channel.messages[0]

    run = await service.run_cycle(NOW + timedelta(minutes=2))
    assert run.sent == 0
    assert len(channel.messages) == 1


async def test_daily_digest_is_sent_once(repo, user, tz, course_factory) -> None:
    seed_course(repo, user, course_factory())
    channel = FakeChannel()
    service = make_service(repo, user, tz, channel)
    digest_now = datetime(2026, 11, 9, 11, 1, tzinfo=UTC)
    service.mark_sync_success(digest_now)

    run = await service.run_cycle(digest_now)
    assert run.baseline is True

    run = await service.run_cycle(digest_now + timedelta(minutes=1))
    assert run.sent == 1
    assert channel.messages[0].startswith("Resumo diário")

    run = await service.run_cycle(digest_now + timedelta(minutes=2))
    assert run.sent == 0
    assert len(channel.messages) == 1


async def test_stale_sync_blocks_notifications(repo, user, tz, course_factory) -> None:
    seed_course(repo, user, course_factory())
    channel = FakeChannel()
    service = make_service(repo, user, tz, channel)
    service.mark_sync_success(NOW)
    await service.run_cycle(NOW)

    service.mark_sync_success(NOW - timedelta(minutes=60))
    run = await service.run_cycle(NOW + timedelta(minutes=1))

    assert run.reason == "stale_sync"
    assert channel.messages == []


async def test_disabled_service_sends_nothing(repo, user, tz, course_factory) -> None:
    seed_course(repo, user, course_factory())
    channel = FakeChannel()
    service = make_service(repo, user, tz, channel, enabled=False)
    service.mark_sync_success(NOW)

    run = await service.run_cycle(NOW)

    assert run.reason == "disabled"
    assert channel.messages == []


async def test_send_test_uses_channel(repo, user, tz) -> None:
    channel = FakeChannel()
    service = make_service(repo, user, tz, channel)

    result = await service.send_test()

    assert "enviado" in result
    assert channel.messages
