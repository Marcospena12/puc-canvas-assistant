from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Course:
    id: int
    name: str
    course_code: str
    workflow_state: str
    term_id: int | None
    term_name: str | None
    term_start_at: str | None
    term_end_at: str | None
    current_score: float | None
    current_grade: str | None
    teachers: list[str] = field(default_factory=list)
    tabs: list[dict[str, Any]] = field(default_factory=list)
    is_subject: bool = True


@dataclass(slots=True)
class Assignment:
    id: int
    course_id: int
    name: str
    due_at: str | None
    lock_at: str | None
    unlock_at: str | None
    points_possible: float | None
    submission_types: list[str]
    is_quiz: bool
    html_url: str | None
    published: bool
    submission_state: str | None
    submission_score: float | None
    submission_grade: str | None
    submitted_at: str | None
    graded_at: str | None
    late: bool
    missing: bool
    excused: bool


@dataclass(slots=True)
class PlannerItem:
    plannable_type: str
    plannable_id: str
    course_id: int | None
    context_name: str | None
    title: str | None
    plannable_date: str | None
    due_at: str | None
    points_possible: float | None
    html_url: str | None
    submitted: bool
    graded: bool
    late: bool
    missing: bool
    new_activity: bool


@dataclass(slots=True)
class Announcement:
    id: int
    course_id: int
    title: str
    message_html: str | None
    author: str | None
    posted_at: str | None
    url: str | None
    read_state: str | None


@dataclass(slots=True)
class CourseFile:
    id: int
    course_id: int
    display_name: str
    filename: str
    content_type: str | None
    size: int | None
    created_at: str | None
    updated_at: str | None
    hidden: bool
    locked: bool
    url: str | None
    mime_class: str | None


@dataclass(slots=True)
class Module:
    id: int
    course_id: int
    name: str
    position: int | None
    state: str | None
    unlock_at: str | None
    require_sequential_progress: bool
    items_count: int


@dataclass(slots=True)
class ModuleItem:
    id: int
    course_id: int
    module_id: int
    title: str
    type: str
    position: int | None
    html_url: str | None
    url: str | None
    external_url: str | None
    page_url: str | None
    content_id: int | None
