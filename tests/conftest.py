from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from canvas_assistant.config import UserProfile
from canvas_assistant.db import Database
from canvas_assistant.models import Assignment, Course
from canvas_assistant.repository import Repository
from canvas_assistant.timeutil import to_utc_iso, utc_now


@pytest.fixture
def tz() -> ZoneInfo:
    return ZoneInfo("America/Sao_Paulo")


@pytest.fixture
def repo(tmp_path) -> Repository:
    database = Database(tmp_path / "test.sqlite3")
    database.initialize()
    return Repository(database)


@pytest.fixture
def user() -> UserProfile:
    return UserProfile(
        user_id="test-user",
        canvas_base_url="https://canvas.test",
        canvas_token="token",
        telegram_chat_id=10,
        telegram_user_ids=frozenset({10}),
    )


@pytest.fixture
def course_factory():
    def make(**overrides):
        now = utc_now()
        data = {
            "id": 1,
            "name": "Cálculo I",
            "course_code": "(12345)",
            "workflow_state": "available",
            "term_id": 1,
            "term_name": "2026/2",
            "term_start_at": to_utc_iso(now - timedelta(days=30)),
            "term_end_at": to_utc_iso(now + timedelta(days=60)),
            "current_score": 80.0,
            "current_grade": None,
            "teachers": ["Prof. Teste"],
            "tabs": [
                {"id": "files", "label": "Arquivos", "hidden": False},
                {"id": "announcements", "label": "Avisos", "hidden": False},
            ],
            "is_subject": True,
        }
        data.update(overrides)
        return Course(**data)

    return make


@pytest.fixture
def assignment_factory():
    def make(**overrides):
        data = {
            "id": 100,
            "course_id": 1,
            "name": "Lista 1",
            "due_at": to_utc_iso(utc_now() + timedelta(days=1)),
            "lock_at": None,
            "unlock_at": None,
            "points_possible": 10.0,
            "submission_types": ["online_upload"],
            "is_quiz": False,
            "html_url": "https://canvas.test/assignments/100",
            "published": True,
            "submission_state": "unsubmitted",
            "submission_score": None,
            "submission_grade": None,
            "submitted_at": None,
            "graded_at": None,
            "late": False,
            "missing": False,
            "excused": False,
        }
        data.update(overrides)
        return Assignment(**data)

    return make
