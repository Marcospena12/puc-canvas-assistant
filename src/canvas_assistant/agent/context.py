from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from ..config import UserProfile
from ..htmltext import html_to_text
from ..repository import Repository
from ..textutil import short_course_name
from ..timeutil import format_local, local_now, parse_dt, to_utc_iso, utc_now
from ..urls import normalize_url


@dataclass(frozen=True, slots=True)
class ContextPack:
    text: str
    urls: frozenset[str]


@dataclass(frozen=True, slots=True)
class Evidence:
    text: str
    urls: frozenset[str]


def _assignment_state(row: dict, now: datetime) -> str:
    if row.get("excused"):
        return "dispensada"
    if row.get("submitted_at"):
        return "entregue"
    due = parse_dt(row.get("due_at"))
    if due is not None and due < now:
        return "atrasada"
    return "pendente"


def build_context_pack(
    repo: Repository,
    user: UserProfile,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> ContextPack:
    current = local_now(tz) if now is None else now.astimezone(tz)
    subjects = repo.get_courses(user.user_id, current_only=True, include_non_subjects=False)
    if not subjects:
        subjects = repo.get_courses(user.user_id, include_non_subjects=False)
    subject_ids = [int(course["id"]) for course in subjects]

    lines = ["DADOS ATUAIS DO CACHE (fonte confiável para hoje e amanhã):"]
    urls: set[str] = set()

    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=2)
    rows = (
        repo.get_upcoming_assignments(
            user.user_id,
            to_utc_iso(start),
            to_utc_iso(end),
            subject_ids,
        )
        if subject_ids
        else []
    )
    if rows:
        for row in rows[:12]:
            link = row.get("html_url")
            if link:
                urls.add(normalize_url(link))
            lines.append(
                "- {due} | {course} | {title} | {status} | {points} | {link}".format(
                    due=format_local(row.get("due_at"), tz),
                    course=short_course_name(row.get("course_name")),
                    title=row.get("name"),
                    status=_assignment_state(row, utc_now()),
                    points=(
                        f"{row['points_possible']:g} pts"
                        if row.get("points_possible") is not None
                        else "sem pontuação"
                    ),
                    link=link or "sem link",
                )
            )
        if len(rows) > 12:
            lines.append(f"- ... e mais {len(rows) - 12} tarefa(s).")
    else:
        lines.append("- Nenhuma tarefa com prazo hoje ou amanhã.")

    announcement_courses = repo.get_courses(
        user.user_id, current_only=True, include_non_subjects=True
    )
    if not announcement_courses:
        announcement_courses = repo.get_courses(user.user_id, include_non_subjects=True)
    announcement_ids = [int(course["id"]) for course in announcement_courses]
    announcements = (
        repo.get_announcements(
            user.user_id,
            since_iso=to_utc_iso(utc_now() - timedelta(days=7)),
            course_ids=announcement_ids,
            limit=5,
        )
        if announcement_ids
        else []
    )
    if announcements:
        lines.append("Avisos recentes (últimos 7 dias):")
        for row in announcements:
            link = row.get("url")
            if link:
                urls.add(normalize_url(link))
            snippet = html_to_text(row.get("message_html"), limit=100)
            lines.append(
                "- {posted} | {course} | {title} | {snippet} | {link}".format(
                    posted=format_local(row.get("posted_at"), tz),
                    course=short_course_name(row.get("course_name")),
                    title=row.get("title"),
                    snippet=snippet,
                    link=link or "sem link",
                )
            )
    else:
        lines.append("Avisos recentes: nenhum nos últimos 7 dias.")

    return ContextPack(text="\n".join(lines), urls=frozenset(urls))
