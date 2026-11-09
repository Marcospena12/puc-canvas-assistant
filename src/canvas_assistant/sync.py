from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .canvas import CanvasError, CanvasService
from .config import UserProfile
from .models import (
    Announcement,
    Assignment,
    Course,
    CourseFile,
    Module,
    ModuleItem,
    PlannerItem,
)
from .repository import Repository
from .timeutil import local_now, parse_dt, to_utc_iso

logger = logging.getLogger(__name__)

_SUBJECT_CODE_RE = re.compile(r"^\(\d{5,}\)")
_TAB_LABELS = {
    "assignments": "tarefas",
    "announcements": "avisos",
    "files": "arquivos",
    "modules": "módulos",
}


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    return bool(value)


def _iso(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    dt = parse_dt(value)
    return to_utc_iso(dt) if dt else None


def _is_subject(course_code: str, name: str) -> bool:
    code = (course_code or "").strip()
    text = f"{code} {name}".casefold()
    return (
        bool(_SUBJECT_CODE_RE.match(code)) or "microfundamento" in text or "eixo" in code.casefold()
    )


def _tab_enabled(tabs: list[dict[str, Any]], tab_id: str) -> bool | None:
    """True/False se a aba existe; None quando não sabemos (tabs indisponíveis)."""
    if not tabs:
        return None
    label = _TAB_LABELS.get(tab_id, tab_id)
    for tab in tabs:
        current_id = str(tab.get("id") or "").casefold()
        current_label = str(tab.get("label") or "").casefold()
        if current_id == tab_id or current_label == label:
            return not bool(tab.get("hidden"))
    return False


def parse_course(raw: dict[str, Any], tabs: list[dict[str, Any]]) -> Course:
    enrollments = raw.get("enrollments") or []
    enrollment: dict[str, Any] = {}
    for item in enrollments:
        if str(item.get("type") or "").casefold() == "studentenrollment":
            enrollment = item
            break
    if not enrollment and enrollments:
        enrollment = enrollments[0]

    term = raw.get("term") or {}
    teachers = [
        str(t.get("display_name") or t.get("name"))
        for t in (raw.get("teachers") or [])
        if t.get("display_name") or t.get("name")
    ]
    course_code = str(raw.get("course_code") or "")
    name = str(raw.get("name") or raw.get("friendly_name") or f"Curso {raw.get('id')}")
    current_grade = enrollment.get("computed_current_grade")
    if current_grade in (None, ""):
        current_grade = enrollment.get("computed_current_letter_grade")

    return Course(
        id=int(raw["id"]),
        name=name,
        course_code=course_code,
        workflow_state=str(raw.get("workflow_state") or ""),
        term_id=_as_int(term.get("id")),
        term_name=term.get("name"),
        term_start_at=_iso(term.get("start_at")),
        term_end_at=_iso(term.get("end_at")),
        current_score=_as_float(enrollment.get("computed_current_score")),
        current_grade=str(current_grade) if current_grade not in (None, "") else None,
        teachers=teachers,
        tabs=tabs,
        is_subject=_is_subject(course_code, name),
    )


def parse_assignment(raw: dict[str, Any], course_id: int) -> Assignment:
    submission = raw.get("submission") or {}
    submission_types = [str(t) for t in (raw.get("submission_types") or [])]
    return Assignment(
        id=int(raw["id"]),
        course_id=course_id,
        name=str(raw.get("name") or "Tarefa sem título"),
        due_at=_iso(raw.get("due_at")),
        lock_at=_iso(raw.get("lock_at")),
        unlock_at=_iso(raw.get("unlock_at")),
        points_possible=_as_float(raw.get("points_possible")),
        submission_types=submission_types,
        is_quiz=_as_bool(raw.get("is_quiz_assignment") or raw.get("quiz_id")),
        html_url=raw.get("html_url"),
        published=_as_bool(raw.get("published", True)),
        submission_state=submission.get("workflow_state"),
        submission_score=_as_float(submission.get("score")),
        submission_grade=(
            str(submission["grade"]) if submission.get("grade") not in (None, "") else None
        ),
        submitted_at=_iso(submission.get("submitted_at")),
        graded_at=_iso(submission.get("graded_at")),
        late=_as_bool(submission.get("late")),
        missing=_as_bool(submission.get("missing")),
        excused=_as_bool(submission.get("excused")),
    )


def parse_planner_item(raw: dict[str, Any]) -> PlannerItem:
    plannable = raw.get("plannable") or {}
    submissions = raw.get("submissions")
    submissions = submissions if isinstance(submissions, dict) else {}
    plannable_id = raw.get("plannable_id")
    return PlannerItem(
        plannable_type=str(raw.get("plannable_type") or "unknown"),
        plannable_id=str(plannable_id),
        course_id=_as_int(raw.get("course_id")),
        context_name=raw.get("context_name"),
        title=plannable.get("title") or plannable.get("name") or raw.get("context_name"),
        plannable_date=_iso(raw.get("plannable_date")),
        due_at=_iso(plannable.get("due_at") or plannable.get("todo_date")),
        points_possible=_as_float(plannable.get("points_possible")),
        html_url=raw.get("html_url"),
        submitted=_as_bool(submissions.get("submitted")),
        graded=_as_bool(submissions.get("graded")),
        late=_as_bool(submissions.get("late")),
        missing=_as_bool(submissions.get("missing")),
        new_activity=_as_bool(raw.get("new_activity")),
    )


def parse_announcement(raw: dict[str, Any], course_id: int) -> Announcement:
    author = raw.get("user_name")
    if not author and isinstance(raw.get("author"), dict):
        author = raw["author"].get("display_name") or raw["author"].get("name")
    return Announcement(
        id=int(raw["id"]),
        course_id=course_id,
        title=str(raw.get("title") or "Aviso sem título"),
        message_html=raw.get("message"),
        author=str(author) if author else None,
        posted_at=_iso(raw.get("posted_at") or raw.get("delayed_post_at") or raw.get("created_at")),
        url=raw.get("html_url") or raw.get("url"),
        read_state=raw.get("read_state"),
    )


def parse_file(raw: dict[str, Any], course_id: int) -> CourseFile:
    content_type = raw.get("content-type") or raw.get("content_type")
    return CourseFile(
        id=int(raw["id"]),
        course_id=course_id,
        display_name=str(raw.get("display_name") or raw.get("filename") or raw["id"]),
        filename=str(raw.get("filename") or ""),
        content_type=content_type,
        size=_as_int(raw.get("size")),
        created_at=_iso(raw.get("created_at")),
        updated_at=_iso(raw.get("modified_at") or raw.get("updated_at")),
        hidden=_as_bool(raw.get("hidden") or raw.get("hidden_for_user")),
        locked=_as_bool(raw.get("locked") or raw.get("locked_for_user")),
        url=raw.get("url"),
        mime_class=raw.get("mime_class"),
    )


def parse_modules(
    course_id: int, raw_modules: list[dict[str, Any]]
) -> tuple[list[Module], list[ModuleItem]]:
    modules: list[Module] = []
    items: list[ModuleItem] = []
    for raw in raw_modules:
        module_id = int(raw["id"])
        raw_items = raw.get("items") or []
        modules.append(
            Module(
                id=module_id,
                course_id=course_id,
                name=str(raw.get("name") or f"Módulo {module_id}"),
                position=_as_int(raw.get("position")),
                state=raw.get("state") or raw.get("workflow_state"),
                unlock_at=_iso(raw.get("unlock_at")),
                require_sequential_progress=_as_bool(raw.get("require_sequential_progress")),
                items_count=_as_int(raw.get("items_count")) or len(raw_items),
            )
        )
        for raw_item in raw_items:
            items.append(
                ModuleItem(
                    id=int(raw_item["id"]),
                    course_id=course_id,
                    module_id=module_id,
                    title=str(raw_item.get("title") or "Item sem título"),
                    type=str(raw_item.get("type") or "Unknown"),
                    position=_as_int(raw_item.get("position")),
                    html_url=raw_item.get("html_url"),
                    url=raw_item.get("url"),
                    external_url=raw_item.get("external_url"),
                    page_url=raw_item.get("page_url"),
                    content_id=_as_int(raw_item.get("content_id")),
                )
            )
    return modules, items


@dataclass
class SyncResult:
    courses: int = 0
    assignments: int = 0
    planner_items: int = 0
    announcements: int = 0
    files: int = 0
    modules: int = 0
    issues: list[str] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        lines = [
            f"cursos: {self.courses}",
            f"tarefas: {self.assignments}",
            f"itens do planner: {self.planner_items}",
            f"avisos: {self.announcements}",
            f"arquivos: {self.files}",
            f"módulos: {self.modules}",
        ]
        if self.issues:
            lines.append("avisos de falha:")
            lines.extend(f"- {issue}" for issue in self.issues[:10])
        return lines


class SyncService:
    def __init__(
        self,
        canvas: CanvasService,
        repo: Repository,
        user: UserProfile,
        tz: ZoneInfo,
        *,
        past_days: int = 7,
        future_days: int = 30,
        announcements_lookback_days: int = 60,
    ) -> None:
        self.canvas = canvas
        self.repo = repo
        self.user = user
        self.tz = tz
        self.past_days = past_days
        self.future_days = future_days
        self.announcements_lookback_days = announcements_lookback_days

    def _record_issue(self, resource: str, scope_id: int, exc: Exception) -> None:
        status = getattr(exc, "status_code", None)
        message = str(exc) or exc.__class__.__name__
        self.repo.add_issue(self.user.user_id, resource, message, scope_id, status)
        logger.warning("Falha em %s (curso=%s, status=%s): %s", resource, scope_id, status, message)

    async def sync_courses(self) -> list[Course]:
        self.repo.clear_issues(self.user.user_id, "courses", 0)
        raw_courses = await self.canvas.get_courses()
        courses: list[Course] = []
        for raw in raw_courses:
            course_id = int(raw["id"])
            tabs: list[dict[str, Any]] = []
            try:
                tabs = await self.canvas.get_tabs(course_id)
                self.repo.clear_issues(self.user.user_id, "tabs", course_id)
            except CanvasError as exc:
                self._record_issue("tabs", course_id, exc)
            courses.append(parse_course(raw, tabs))
        self.repo.upsert_courses(self.user.user_id, courses)
        logger.info("Cursos sincronizados: %s", len(courses))
        return courses

    async def sync_planner(self) -> int:
        now = local_now(self.tz)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = (today - timedelta(days=self.past_days)).date().isoformat()
        end = (today + timedelta(days=self.future_days)).date().isoformat()
        try:
            raw_items = await self.canvas.get_planner_items(start, end)
            items = [parse_planner_item(raw) for raw in raw_items]
            self.repo.upsert_planner_items(self.user.user_id, items)
            self.repo.clear_issues(self.user.user_id, "planner", 0)
            return len(items)
        except CanvasError as exc:
            self._record_issue("planner", 0, exc)
            return 0

    async def sync_course_details(self, course: Course) -> tuple[int, int, int, int]:
        assignments = announcements = files = modules = 0
        user_id = self.user.user_id

        if _tab_enabled(course.tabs, "assignments") is not False:
            try:
                raw = await self.canvas.get_assignments(course.id)
                parsed = [parse_assignment(item, course.id) for item in raw]
                self.repo.replace_assignments(user_id, course.id, parsed)
                self.repo.clear_issues(user_id, "assignments", course.id)
                assignments = len(parsed)
            except CanvasError as exc:
                self._record_issue("assignments", course.id, exc)

        if _tab_enabled(course.tabs, "announcements") is not False:
            now = local_now(self.tz)
            start = (now - timedelta(days=self.announcements_lookback_days)).date().isoformat()
            end = (now + timedelta(days=1)).date().isoformat()
            try:
                raw = await self.canvas.get_announcements(course.id, start, end)
                parsed = [parse_announcement(item, course.id) for item in raw]
                self.repo.upsert_announcements(user_id, parsed)
                self.repo.clear_issues(user_id, "announcements", course.id)
                announcements = len(parsed)
            except CanvasError as exc:
                self._record_issue("announcements", course.id, exc)

        if _tab_enabled(course.tabs, "files") is not False:
            try:
                raw = await self.canvas.get_files(course.id)
                parsed = [parse_file(item, course.id) for item in raw]
                self.repo.replace_files(user_id, course.id, parsed)
                self.repo.clear_issues(user_id, "files", course.id)
                files = len(parsed)
            except CanvasError as exc:
                self._record_issue("files", course.id, exc)

        if _tab_enabled(course.tabs, "modules") is not False:
            try:
                raw = await self.canvas.get_modules(course.id)
                parsed_modules, parsed_items = parse_modules(course.id, raw)
                self.repo.replace_modules(user_id, course.id, parsed_modules, parsed_items)
                self.repo.clear_issues(user_id, "modules", course.id)
                modules = len(parsed_modules)
            except CanvasError as exc:
                self._record_issue("modules", course.id, exc)

        logger.info(
            "Curso %s: %s tarefas, %s avisos, %s arquivos, %s módulos",
            course.id,
            assignments,
            announcements,
            files,
            modules,
        )
        return assignments, announcements, files, modules

    async def sync_all(self) -> SyncResult:
        result = SyncResult()
        courses = await self.sync_courses()
        result.courses = len(courses)
        result.planner_items = await self.sync_planner()

        for course in courses:
            assignments, announcements, files, modules = await self.sync_course_details(course)
            result.assignments += assignments
            result.announcements += announcements
            result.files += files
            result.modules += modules

        issues = self.repo.list_issues(self.user.user_id, limit=10)
        result.issues = [
            f"{issue['resource']} (curso {issue['scope_id']}): {issue['message']}"
            for issue in issues
        ]
        return result
