from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings, UserProfile
from .notifications import _clean
from .repository import Repository
from .textutil import short_course_name
from .timeutil import format_local, parse_dt, to_utc_iso, utc_now

logger = logging.getLogger(__name__)

SCHEDULER_LAST_CYCLE_PREFIX = "scheduler:last_cycle"
PROCESS_STARTED_AT_PREFIX = "process:started_at"
CATCHUP_META_PREFIX = "catchup:last"


def _last_cycle_key(user_id: str) -> str:
    return f"{SCHEDULER_LAST_CYCLE_PREFIX}:{user_id}"


def _started_at_key(user_id: str) -> str:
    return f"{PROCESS_STARTED_AT_PREFIX}:{user_id}"


def _catchup_sent_key(user_id: str, cycle_iso: str) -> str:
    safe = cycle_iso.replace(":", "-").replace("+", "Z")
    return f"{CATCHUP_META_PREFIX}:{user_id}:{safe}"


def mark_scheduler_cycle(repo: Repository, user_id: str, now: datetime | None = None) -> None:
    from datetime import UTC

    current = (now or utc_now()).astimezone(UTC)
    repo.set_meta(_last_cycle_key(user_id), to_utc_iso(current))


def get_last_cycle(repo: Repository, user_id: str) -> datetime | None:
    return parse_dt(repo.get_meta(_last_cycle_key(user_id)))


def persist_started_at(repo: Repository, user_id: str, started_at: datetime) -> None:
    from datetime import UTC

    repo.set_meta(_started_at_key(user_id), to_utc_iso(started_at.astimezone(UTC)))


def get_started_at(repo: Repository, user_id: str) -> datetime | None:
    return parse_dt(repo.get_meta(_started_at_key(user_id)))


def _format_gap(gap: timedelta) -> str:
    total_minutes = int(gap.total_seconds() / 60)
    if total_minutes < 60:
        return f"{total_minutes} min"
    hours = total_minutes / 60
    if hours < 24:
        h = int(gap.total_seconds() // 3600)
        m = (int(gap.total_seconds()) % 3600) // 60
        return f"{h}h{m:02d}min" if m else f"{h}h"
    days = gap.days
    rem_hours = int((gap.total_seconds() - days * 86400) // 3600)
    return f"{days}d{rem_hours}h" if rem_hours else f"{days}d"


def _subject_course_ids(repo: Repository, user: UserProfile) -> list[int]:
    courses = repo.get_courses(user.user_id, current_only=True, include_non_subjects=False)
    if not courses:
        courses = repo.get_courses(user.user_id, include_non_subjects=False)
    return [int(c["id"]) for c in courses]


def _announcement_course_ids(repo: Repository, user: UserProfile) -> list[int]:
    courses = repo.get_courses(user.user_id, current_only=True, include_non_subjects=True)
    if not courses:
        courses = repo.get_courses(user.user_id, include_non_subjects=True)
    return [int(c["id"]) for c in courses]


def _task_line(row: dict[str, Any], tz: ZoneInfo) -> str:
    link = row.get("html_url")
    parts = [
        format_local(row.get("due_at"), tz),
        short_course_name(row.get("course_name")),
        _clean(row.get("name")),
    ]
    if link:
        parts.append("[abrir]")
    return " | ".join(parts)


def _announcement_line(row: dict[str, Any], tz: ZoneInfo) -> str:
    link = row.get("url")
    parts = [
        format_local(row.get("posted_at"), tz),
        short_course_name(row.get("course_name")),
        _clean(row.get("title")),
    ]
    if link:
        parts.append("[abrir]")
    return " | ".join(parts)


def build_catchup_message(
    repo: Repository,
    user: UserProfile,
    settings: Settings,
    tz: ZoneInfo,
    *,
    last_cycle: datetime,
    now: datetime,
) -> str:
    """Constrói a mensagem consolidada de catch-up.

    Formato: "Fiquei offline de X a Y. Pendências: ..."
    """
    gap = now - last_cycle
    gap_label = _format_gap(gap)
    lines = [
        f"Fiquei offline de {format_local(last_cycle, tz)} a {format_local(now, tz)} ({gap_label})."
    ]

    course_ids = _subject_course_ids(repo, user)

    overdue_in_gap: list[dict[str, Any]] = []
    if course_ids:
        rows = repo.get_upcoming_assignments(
            user.user_id,
            to_utc_iso(last_cycle),
            to_utc_iso(now),
            course_ids,
        )
        for row in rows:
            if row.get("submitted_at") or row.get("excused"):
                continue
            overdue_in_gap.append(row)

    upcoming_24h: list[dict[str, Any]] = []
    if course_ids:
        rows = repo.get_upcoming_assignments(
            user.user_id,
            to_utc_iso(now),
            to_utc_iso(now + timedelta(hours=24)),
            course_ids,
        )
        for row in rows:
            if row.get("submitted_at") or row.get("excused"):
                continue
            upcoming_24h.append(row)

    ann_course_ids = _announcement_course_ids(repo, user)
    announcements: list[dict[str, Any]] = []
    if ann_course_ids:
        announcements = repo.get_announcements(
            user.user_id,
            since_iso=to_utc_iso(last_cycle),
            course_ids=ann_course_ids,
            limit=5,
        )

    new_files = repo.list_unnotified_files(user.user_id, limit=10)

    pendencias: list[str] = []

    if overdue_in_gap:
        pendencias.append("Vencidas na lacuna:")
        for row in overdue_in_gap[:10]:
            pendencias.append("- " + _task_line(row, tz))

    if upcoming_24h:
        pendencias.append("Vencem nas próximas 24h:")
        for row in upcoming_24h[:10]:
            pendencias.append("- " + _task_line(row, tz))

    if announcements:
        pendencias.append("Avisos:")
        for row in announcements:
            pendencias.append("- " + _announcement_line(row, tz))

    if new_files:
        pendencias.append("Arquivos novos:")
        for row in new_files[:10]:
            course = short_course_name(row.get("course_name"))
            name = _clean(row.get("display_name"))
            pendencias.append(f"- {course}: {name}")

    if not pendencias:
        lines.append("Nenhuma pendência nova no período.")
    else:
        lines.append("Pendências:")
        lines.extend(pendencias)

    return "\n".join(lines)


def should_run_catchup(
    repo: Repository,
    user_id: str,
    gap_minutes: int,
    now: datetime | None = None,
) -> tuple[bool, datetime | None, datetime]:
    current = now or utc_now()
    last = get_last_cycle(repo, user_id)
    if last is None:
        return False, None, current
    gap = current - last
    if gap <= timedelta(minutes=gap_minutes):
        return False, last, current
    return True, last, current


def already_sent_catchup(repo: Repository, user_id: str, cycle_iso: str) -> bool:
    return repo.get_meta(_catchup_sent_key(user_id, cycle_iso)) == "1"


def mark_catchup_sent(repo: Repository, user_id: str, cycle_iso: str) -> None:
    repo.set_meta(_catchup_sent_key(user_id, cycle_iso), "1")
