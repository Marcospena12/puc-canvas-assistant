from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from .config import NotificationConfig, UserProfile
from .htmltext import html_to_text
from .repository import Repository
from .textutil import short_course_name
from .timeutil import WEEKDAYS_PT, format_local, parse_dt, to_utc_iso, utc_now

logger = logging.getLogger(__name__)

DUE_EVENT_PREFIX = "due"
SYNC_META_PREFIX = "notify_last_sync"
BASELINE_META_PREFIX = "notify_baseline"
ENABLED_META_PREFIX = "notify_enabled_state"


class Notifier(Protocol):
    async def send_message(self, user: UserProfile, text: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class NotificationRun:
    sent: int = 0
    baseline: bool = False
    reason: str | None = None


def _event(key: str, kind: str, resource_id: str | int | None) -> tuple[str, str, str | None]:
    return key, kind, str(resource_id) if resource_id is not None else None


def _clean(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return text[: limit - 1].rstrip() + "…"
    return text


def _points(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:g} pts"
    return str(value)


def _sync_meta_key(user_id: str) -> str:
    return f"{SYNC_META_PREFIX}:{user_id}"


def _baseline_meta_key(user_id: str) -> str:
    return f"{BASELINE_META_PREFIX}:{user_id}"


def _enabled_meta_key(user_id: str) -> str:
    return f"{ENABLED_META_PREFIX}:{user_id}"


class NotificationService:
    def __init__(
        self,
        repo: Repository,
        user: UserProfile,
        tz: ZoneInfo,
        channel: Notifier,
        config: NotificationConfig,
    ) -> None:
        self.repo = repo
        self.user = user
        self.tz = tz
        self.channel = channel
        self.config = config
        self._lock = asyncio.Lock()

    def mark_sync_success(self, now: datetime | None = None) -> None:
        current = (now or utc_now()).astimezone(UTC)
        self.repo.set_meta(_sync_meta_key(self.user.user_id), to_utc_iso(current))

    def initialize(self, now: datetime | None = None) -> bool:
        current = (now or utc_now()).astimezone(UTC)
        if not self.config.enabled:
            self.repo.set_meta(_enabled_meta_key(self.user.user_id), "0")
            return False
        if self.repo.is_empty(self.user.user_id):
            return False
        if self.repo.get_meta(_enabled_meta_key(self.user.user_id)) != "1":
            self._baseline(current)
            self.repo.set_meta(_enabled_meta_key(self.user.user_id), "1")
            return True
        return False

    def _baseline(self, now: datetime) -> None:
        user_id = self.user.user_id
        assignment_ids = self.repo.list_assignment_ids(user_id)
        announcement_ids = self.repo.list_announcement_ids(user_id)
        events = [
            _event(f"new_assignment:{assignment_id}", "baseline", assignment_id)
            for assignment_id in assignment_ids
        ]
        events.extend(
            _event(f"new_announcement:{announcement_id}", "baseline", announcement_id)
            for announcement_id in announcement_ids
        )
        self.repo.add_notification_events(user_id, events)
        self.repo.set_meta(_baseline_meta_key(user_id), to_utc_iso(now))
        logger.info(
            "Baseline de notificações: %s tarefas e %s avisos",
            len(assignment_ids),
            len(announcement_ids),
        )

    def is_quiet(self, now: datetime) -> bool:
        local_time = now.astimezone(self.tz).time()
        start = self.config.quiet_start
        end = self.config.quiet_end
        if start == end:
            return False
        if start < end:
            return start <= local_time < end
        return local_time >= start or local_time < end

    def _sync_is_fresh(self, now: datetime) -> bool:
        last = parse_dt(self.repo.get_meta(_sync_meta_key(self.user.user_id)))
        if last is None:
            return False
        return (now - last).total_seconds() <= self.config.max_stale_minutes * 60

    def _subject_course_ids(self) -> list[int]:
        courses = self.repo.get_courses(
            self.user.user_id, current_only=True, include_non_subjects=False
        )
        if not courses:
            courses = self.repo.get_courses(self.user.user_id, include_non_subjects=False)
        return [int(course["id"]) for course in courses]

    def _announcement_course_ids(self) -> list[int]:
        courses = self.repo.get_courses(
            self.user.user_id, current_only=True, include_non_subjects=True
        )
        if not courses:
            courses = self.repo.get_courses(self.user.user_id, include_non_subjects=True)
        return [int(course["id"]) for course in courses]

    async def run_cycle(self, now: datetime | None = None) -> NotificationRun:
        async with self._lock:
            current = (now or utc_now()).astimezone(UTC)
            if not self.config.enabled:
                self.repo.set_meta(_enabled_meta_key(self.user.user_id), "0")
                return NotificationRun(reason="disabled")
            if self.repo.is_empty(self.user.user_id):
                return NotificationRun(reason="empty_cache")
            if self.initialize(current):
                return NotificationRun(baseline=True, reason="baseline")
            if not self._sync_is_fresh(current):
                return NotificationRun(reason="stale_sync")
            if self.is_quiet(current):
                return NotificationRun(reason="quiet_hours")

            sent = 0
            if self.config.new_assignments:
                sent += await self._send_new_assignments()
            if self.config.announcements:
                sent += await self._send_new_announcements()
            sent += await self._send_due_reminders(current)
            if self.config.digest:
                sent += await self._send_digest(current)

            if sent:
                logger.info("Notificações enviadas: %s", sent)
            return NotificationRun(sent=sent)

    async def _send(
        self,
        text: str,
        events: Sequence[tuple[str, str, str | None]],
    ) -> bool:
        if not events:
            return False
        try:
            ok = await self.channel.send_message(self.user, text)
        except Exception:
            logger.exception("Falha ao enviar notificação")
            return False
        if not ok:
            logger.warning("Notificação não entregue; será tentada novamente")
            return False
        self.repo.add_notification_events(self.user.user_id, events)
        return True

    async def _send_new_assignments(self) -> int:
        rows = self.repo.list_unnotified_assignments(self.user.user_id, limit=20)
        if not rows:
            return 0
        skipped = [row for row in rows if row.get("submitted_at") or row.get("excused")]
        if skipped:
            self.repo.add_notification_events(
                self.user.user_id,
                [
                    _event(f"new_assignment:{row['id']}", "assignment_skipped", row["id"])
                    for row in skipped
                ],
            )
        visible = [row for row in rows if row not in skipped]
        if not visible:
            return 0

        lines = ["Novas tarefas no Canvas:"]
        for row in visible:
            link = row.get("html_url")
            parts = [
                format_local(row.get("due_at"), self.tz),
                short_course_name(row.get("course_name")),
                _clean(row.get("name")),
            ]
            points = _points(row.get("points_possible"))
            if points:
                parts.append(points)
            if link:
                parts.append(f"[abrir]({link})")
            lines.append("- " + " | ".join(parts))

        events = [
            _event(f"new_assignment:{row['id']}", "new_assignment", row["id"]) for row in visible
        ]
        return 1 if await self._send("\n".join(lines), events) else 0

    async def _send_new_announcements(self) -> int:
        rows = self.repo.list_unnotified_announcements(self.user.user_id, limit=10)
        if not rows:
            return 0

        lines = ["Novos avisos no Canvas:"]
        for row in rows:
            link = row.get("url")
            parts = [
                format_local(row.get("posted_at"), self.tz),
                short_course_name(row.get("course_name")),
                _clean(row.get("title")),
            ]
            snippet = html_to_text(row.get("message_html"), limit=160)
            if snippet:
                parts.append(_clean(snippet))
            if link:
                parts.append(f"[abrir]({link})")
            lines.append("- " + " | ".join(parts))

        events = [
            _event(f"new_announcement:{row['id']}", "new_announcement", row["id"]) for row in rows
        ]
        return 1 if await self._send("\n".join(lines), events) else 0

    async def _send_due_reminders(self, now: datetime) -> int:
        course_ids = self._subject_course_ids()
        if not course_ids:
            return 0
        max_hours = max(self.config.due_hours)
        rows = self.repo.get_upcoming_assignments(
            self.user.user_id,
            to_utc_iso(now - timedelta(hours=1)),
            to_utc_iso(now + timedelta(hours=max_hours + 1)),
            course_ids,
        )

        candidates: list[tuple[datetime, str, int, dict[str, Any]]] = []
        for row in rows:
            if row.get("submitted_at") or row.get("excused"):
                continue
            due = parse_dt(row.get("due_at"))
            if due is None or due <= now:
                continue
            eligible = [
                hours for hours in self.config.due_hours if now >= due - timedelta(hours=hours)
            ]
            if not eligible:
                continue
            threshold = min(eligible)
            key = f"{DUE_EVENT_PREFIX}:{row['id']}:{threshold}:{due.astimezone(UTC).isoformat()}"
            candidates.append((due, key, threshold, row))

        if not candidates:
            return 0
        existing = self.repo.get_notification_event_keys(
            self.user.user_id, [key for _, key, _, _ in candidates]
        )
        pending = [(due, key, row) for due, key, _, row in candidates if key not in existing]
        if not pending:
            return 0
        pending.sort(key=lambda item: item[0])
        selected = pending[:20]

        lines = ["Prazos próximos:"]
        events = []
        for _, key, row in selected:
            link = row.get("html_url")
            parts = [
                format_local(row.get("due_at"), self.tz),
                short_course_name(row.get("course_name")),
                _clean(row.get("name")),
            ]
            points = _points(row.get("points_possible"))
            if points:
                parts.append(points)
            if link:
                parts.append(f"[abrir]({link})")
            lines.append("- " + " | ".join(parts))
            threshold = key.split(":")[2]
            events.append(_event(key, f"due_reminder_{threshold}h", row["id"]))

        return 1 if await self._send("\n".join(lines), events) else 0

    def _digest_is_due(self, now: datetime) -> bool:
        local = now.astimezone(self.tz)
        target = datetime.combine(local.date(), self.config.digest_time).replace(tzinfo=self.tz)
        grace = max(self.config.digest_grace_minutes, 1)
        return target <= local < target + timedelta(minutes=grace)

    async def _send_digest(self, now: datetime) -> int:
        if not self._digest_is_due(now):
            return 0
        local = now.astimezone(self.tz)
        event_key = f"digest:{local.date().isoformat()}"
        if self.repo.has_notification_event(self.user.user_id, event_key):
            return 0

        course_ids = self._subject_course_ids()
        text = self._build_digest(now, local, course_ids)
        return 1 if await self._send(text, [_event(event_key, "digest", None)]) else 0

    def _build_digest(self, now: datetime, local: datetime, course_ids: list[int]) -> str:
        lines = [f"Resumo diário - {WEEKDAYS_PT[local.weekday()]} {local:%d/%m}"]

        upcoming: list[dict[str, Any]] = []
        if course_ids:
            start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            rows = self.repo.get_upcoming_assignments(
                self.user.user_id,
                to_utc_iso(start),
                to_utc_iso(start + timedelta(days=2)),
                course_ids,
            )
            for row in rows:
                if row.get("submitted_at") or row.get("excused"):
                    continue
                due = parse_dt(row.get("due_at"))
                if due is not None and due >= now:
                    upcoming.append(row)

        lines.append("Hoje e amanhã:")
        if upcoming:
            for row in upcoming[:15]:
                lines.append("- " + self._task_line(row))
        else:
            lines.append("- Nenhuma tarefa pendente.")

        overdue: list[dict[str, Any]] = []
        if course_ids:
            rows = self.repo.get_upcoming_assignments(
                self.user.user_id,
                to_utc_iso(now - timedelta(days=7)),
                to_utc_iso(now),
                course_ids,
            )
            overdue = [
                row for row in rows if not row.get("submitted_at") and not row.get("excused")
            ]
        if overdue:
            lines.append("Atrasadas:")
            for row in overdue[:5]:
                lines.append("- " + self._task_line(row))

        ann_course_ids = self._announcement_course_ids()
        announcements = (
            self.repo.get_announcements(
                self.user.user_id,
                since_iso=to_utc_iso(now - timedelta(days=1)),
                course_ids=ann_course_ids,
                limit=5,
            )
            if ann_course_ids
            else []
        )
        if announcements:
            lines.append("Avisos recentes:")
            for row in announcements:
                link = row.get("url")
                parts = [
                    format_local(row.get("posted_at"), self.tz),
                    short_course_name(row.get("course_name")),
                    _clean(row.get("title")),
                ]
                if link:
                    parts.append(f"[abrir]({link})")
                lines.append("- " + " | ".join(parts))

        return "\n".join(lines)

    def _task_line(self, row: dict[str, Any]) -> str:
        link = row.get("html_url")
        parts = [
            format_local(row.get("due_at"), self.tz),
            short_course_name(row.get("course_name")),
            _clean(row.get("name")),
        ]
        points = _points(row.get("points_possible"))
        if points:
            parts.append(points)
        if link:
            parts.append(f"[abrir]({link})")
        return " | ".join(parts)

    async def send_test(self) -> str:
        text = (
            "Teste de notificação do assistente.\n"
            "Se você recebeu esta mensagem, os envios proativos estão funcionando."
        )
        ok = await self.channel.send_message(self.user, text)
        if ok:
            return "Teste de notificação enviado."
        return "Não consegui enviar o teste; veja os logs do bot."
