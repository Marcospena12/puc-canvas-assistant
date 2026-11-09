from __future__ import annotations

import json

from canvas_assistant.agent.tools import ToolRegistry
from canvas_assistant.models import CourseFile, Module, ModuleItem


async def test_get_courses_reports_tab_availability(repo, user, tz, course_factory) -> None:
    repo.upsert_courses(user.user_id, [course_factory()])
    registry = ToolRegistry(repo, user, tz)

    result = json.loads(await registry.execute("get_courses", {}))

    assert result["ok"] is True
    assert result["count"] == 1
    course = result["courses"][0]
    assert course["course_id"] == 1
    assert course["current_term"] is True
    assert course["files_available"] is True
    assert course["announcements_available"] is True


async def test_get_courses_without_cache_returns_note(repo, user, tz) -> None:
    registry = ToolRegistry(repo, user, tz)

    result = json.loads(await registry.execute("get_courses", {}))

    assert result["count"] == 0
    assert result["note"]


async def test_get_assignments_unknown_course_returns_error(repo, user, tz) -> None:
    registry = ToolRegistry(repo, user, tz)

    result = json.loads(await registry.execute("get_assignments", {"course_id": 999}))

    assert result["ok"] is False
    assert "não encontrado" in result["error"]


async def test_list_files_merges_files_and_module_items_without_duplicates(
    repo, user, tz, course_factory
) -> None:
    course = course_factory()
    repo.upsert_courses(user.user_id, [course])
    repo.replace_files(
        user.user_id,
        course.id,
        [
            CourseFile(
                id=7,
                course_id=course.id,
                display_name="Aula 1.pdf",
                filename="aula1.pdf",
                content_type="application/pdf",
                size=2048,
                created_at=None,
                updated_at=None,
                hidden=False,
                locked=False,
                url="https://canvas.test/files/7",
                mime_class="pdf",
            )
        ],
    )
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
                items_count=2,
            )
        ],
        [
            ModuleItem(
                id=30,
                course_id=course.id,
                module_id=3,
                title="Aula 1.pdf",
                type="File",
                position=1,
                html_url=None,
                url=None,
                external_url=None,
                page_url=None,
                content_id=7,
            ),
            ModuleItem(
                id=31,
                course_id=course.id,
                module_id=3,
                title="Aula 2.pdf",
                type="File",
                position=2,
                html_url=None,
                url=None,
                external_url=None,
                page_url=None,
                content_id=8,
            ),
        ],
    )
    registry = ToolRegistry(repo, user, tz)

    result = json.loads(await registry.execute("list_course_files", {}))

    names = [item["name"] for item in result["files"]]
    assert names == ["Aula 1.pdf", "Aula 2.pdf"]
    assert result["count"] == 2
    module_item = result["files"][1]
    assert module_item["source"] == "modules"
    assert module_item["url"] == "https://canvas.test/courses/1/files/8"
