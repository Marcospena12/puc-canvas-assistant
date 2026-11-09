from __future__ import annotations

from canvas_assistant.sync import (
    parse_announcement,
    parse_assignment,
    parse_course,
    parse_file,
    parse_modules,
    parse_planner_item,
)


def test_parse_course_detects_subject_and_term() -> None:
    raw = {
        "id": 10,
        "name": "Cálculo I - Sistemas de Informação",
        "course_code": "(12345)",
        "workflow_state": "available",
        "term": {
            "id": 363,
            "name": "2026/2",
            "start_at": "2026-08-03T03:00:00Z",
            "end_at": "2026-12-31T02:59:59Z",
        },
        "enrollments": [
            {
                "type": "studentenrollment",
                "computed_current_score": 87.5,
                "computed_current_grade": "A",
            }
        ],
        "teachers": [{"display_name": "Prof. Ana"}],
    }

    course = parse_course(raw, [{"id": "files", "label": "Arquivos", "hidden": False}])

    assert course.id == 10
    assert course.term_id == 363
    assert course.term_start_at == "2026-08-03T03:00:00+00:00"
    assert course.current_score == 87.5
    assert course.current_grade == "A"
    assert course.teachers == ["Prof. Ana"]
    assert course.is_subject is True


def test_parse_course_excludes_coordination_space() -> None:
    raw = {
        "id": 11,
        "name": "Espaço da Coordenação - Sistemas de Informação",
        "course_code": "COORD",
        "workflow_state": "available",
        "enrollments": [],
    }

    course = parse_course(raw, [])

    assert course.is_subject is False


def test_parse_course_detects_microfundamento() -> None:
    raw = {
        "id": 12,
        "name": "Eixo 1 - Microfundamento: Algoritmos",
        "course_code": "EAD-1",
        "workflow_state": "available",
        "enrollments": [],
    }

    assert parse_course(raw, []).is_subject is True


def test_parse_assignment_normalizes_due_and_submission() -> None:
    raw = {
        "id": 99,
        "name": "Prova 1",
        "due_at": "2026-11-10T02:59:00Z",
        "points_possible": 20,
        "submission_types": ["online_quiz"],
        "is_quiz_assignment": True,
        "published": True,
        "submission": {
            "workflow_state": "graded",
            "score": 18.5,
            "grade": "18.5",
            "submitted_at": "2026-11-09T20:00:00Z",
            "graded_at": "2026-11-09T21:00:00Z",
        },
    }

    assignment = parse_assignment(raw, course_id=1)

    assert assignment.due_at == "2026-11-10T02:59:00+00:00"
    assert assignment.is_quiz is True
    assert assignment.submission_state == "graded"
    assert assignment.submission_score == 18.5
    assert assignment.graded_at == "2026-11-09T21:00:00+00:00"


def test_parse_file_uses_canvas_content_type_field() -> None:
    raw = {
        "id": 7,
        "display_name": "Aula 1.pdf",
        "filename": "aula1.pdf",
        "content-type": "application/pdf",
        "size": 2048,
        "modified_at": "2026-11-01T12:00:00Z",
        "hidden_for_user": False,
        "locked_for_user": True,
    }

    file = parse_file(raw, course_id=1)

    assert file.content_type == "application/pdf"
    assert file.size == 2048
    assert file.locked is True
    assert file.updated_at == "2026-11-01T12:00:00+00:00"


def test_parse_announcement_and_planner_item() -> None:
    announcement = parse_announcement(
        {
            "id": 5,
            "title": "Aviso",
            "message": "<p>Conteúdo</p>",
            "author": {"display_name": "Prof. Ana"},
            "posted_at": "2026-11-01T12:00:00Z",
        },
        course_id=1,
    )
    assert announcement.author == "Prof. Ana"
    assert announcement.message_html == "<p>Conteúdo</p>"

    item = parse_planner_item(
        {
            "plannable_type": "assignment",
            "plannable_id": 99,
            "course_id": 1,
            "context_name": "Cálculo",
            "plannable": {"title": "Lista 1", "due_at": "2026-11-10T02:59:00Z"},
            "submissions": {"submitted": True},
        }
    )
    assert item.plannable_id == "99"
    assert item.title == "Lista 1"
    assert item.due_at == "2026-11-10T02:59:00+00:00"
    assert item.submitted is True


def test_parse_modules_flattens_items() -> None:
    modules, items = parse_modules(
        1,
        [
            {
                "id": 3,
                "name": "Semana 1",
                "position": 1,
                "items_count": 0,
                "items": [
                    {
                        "id": 30,
                        "title": "Slide",
                        "type": "File",
                        "position": 1,
                        "content_id": 7,
                    },
                    {
                        "id": 31,
                        "title": "Leitura",
                        "type": "Page",
                        "position": 2,
                    },
                ],
            }
        ],
    )

    assert modules[0].items_count == 2
    assert [item.id for item in items] == [30, 31]
    assert items[0].content_id == 7
