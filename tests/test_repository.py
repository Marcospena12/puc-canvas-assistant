from __future__ import annotations

from datetime import timedelta

from canvas_assistant.models import CourseFile, Module, ModuleItem
from canvas_assistant.timeutil import to_utc_iso, utc_now


def test_upsert_courses_marks_missing_as_inactive(repo, user, course_factory) -> None:
    first = course_factory(id=1, name="Matéria 1")
    second = course_factory(id=2, name="Matéria 2")

    repo.upsert_courses(user.user_id, [first, second])
    repo.upsert_courses(user.user_id, [first])

    active = repo.get_courses(user.user_id)
    assert [course["id"] for course in active] == [1]


def test_get_courses_current_only_uses_term_dates(repo, user, course_factory) -> None:
    now = utc_now()
    current = course_factory(id=1, name="Atual")
    old = course_factory(
        id=2,
        name="Antiga",
        term_start_at=to_utc_iso(now - timedelta(days=365)),
        term_end_at=to_utc_iso(now - timedelta(days=200)),
    )
    repo.upsert_courses(user.user_id, [current, old])

    courses = repo.get_courses(user.user_id, current_only=True, now_iso=to_utc_iso(now))

    assert [course["id"] for course in courses] == [1]


def test_get_upcoming_assignments_filters_window(
    repo, user, course_factory, assignment_factory
) -> None:
    now = utc_now()
    course = course_factory()
    repo.upsert_courses(user.user_id, [course])
    repo.replace_assignments(
        user.user_id,
        course.id,
        [
            assignment_factory(id=1, due_at=to_utc_iso(now + timedelta(days=1))),
            assignment_factory(id=2, due_at=to_utc_iso(now - timedelta(days=10))),
            assignment_factory(id=3, due_at=None),
        ],
    )

    rows = repo.get_upcoming_assignments(
        user.user_id,
        to_utc_iso(now - timedelta(days=1)),
        to_utc_iso(now + timedelta(days=2)),
        [course.id],
    )

    assert [row["id"] for row in rows] == [1]


def test_get_grades_counts_graded_missing_and_overdue(
    repo, user, course_factory, assignment_factory
) -> None:
    now = utc_now()
    course = course_factory(current_score=80.0)
    repo.upsert_courses(user.user_id, [course])
    repo.replace_assignments(
        user.user_id,
        course.id,
        [
            assignment_factory(
                id=1,
                due_at=to_utc_iso(now + timedelta(days=1)),
                submission_state="graded",
                submission_score=9.0,
                submitted_at=to_utc_iso(now - timedelta(days=1)),
                graded_at=to_utc_iso(now - timedelta(days=1)),
            ),
            assignment_factory(
                id=2,
                due_at=to_utc_iso(now - timedelta(days=1)),
                missing=True,
                late=True,
            ),
        ],
    )

    grades = repo.get_grades(user.user_id, [course.id])

    assert len(grades) == 1
    assert grades[0]["current_score"] == 80.0
    assert grades[0]["assignments_total"] == 2
    assert grades[0]["graded_count"] == 1
    assert grades[0]["missing_count"] == 1
    assert grades[0]["overdue_count"] == 1


def test_get_assignments_pending_excludes_submitted_and_excused(
    repo, user, course_factory, assignment_factory
) -> None:
    course = course_factory()
    repo.upsert_courses(user.user_id, [course])
    repo.replace_assignments(
        user.user_id,
        course.id,
        [
            assignment_factory(id=1),
            assignment_factory(
                id=2,
                submitted_at=to_utc_iso(utc_now()),
                submission_state="submitted",
            ),
            assignment_factory(id=3, excused=True),
        ],
    )

    rows = repo.get_assignments(user.user_id, course.id, status="pending")

    assert [row["id"] for row in rows] == [1]


def test_replace_files_deduplicates_ids(repo, user, course_factory) -> None:
    course = course_factory()
    repo.upsert_courses(user.user_id, [course])
    file = CourseFile(
        id=7,
        course_id=course.id,
        display_name="Aula 1.pdf",
        filename="aula1.pdf",
        content_type="application/pdf",
        size=1024,
        created_at=None,
        updated_at=None,
        hidden=False,
        locked=False,
        url="https://canvas.test/files/7",
        mime_class="pdf",
    )

    repo.replace_files(user.user_id, course.id, [file, file])

    files = repo.get_files(user.user_id, course_ids=[course.id])
    assert len(files) == 1
    assert files[0]["id"] == 7


def test_cache_stats_and_empty(repo, user, course_factory) -> None:
    assert repo.is_empty(user.user_id) is True

    repo.upsert_courses(user.user_id, [course_factory()])

    assert repo.is_empty(user.user_id) is False
    stats = repo.cache_stats(user.user_id)
    assert stats["courses"]["count"] == 1
    assert stats["assignments"]["count"] == 0


def test_replace_modules_and_items(repo, user, course_factory) -> None:
    course = course_factory()
    repo.upsert_courses(user.user_id, [course])
    repo.replace_modules(
        user.user_id,
        course.id,
        [
            Module(
                id=3,
                course_id=course.id,
                name="Semana 1",
                position=1,
                state="active",
                unlock_at=None,
                require_sequential_progress=False,
                items_count=1,
            )
        ],
        [
            ModuleItem(
                id=30,
                course_id=course.id,
                module_id=3,
                title="Slide",
                type="File",
                position=1,
                html_url=None,
                url=None,
                external_url=None,
                page_url=None,
                content_id=7,
            )
        ],
    )

    items = repo.get_module_file_items(user.user_id, course_ids=[course.id])

    assert len(items) == 1
    assert items[0]["content_id"] == 7
