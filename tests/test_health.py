from __future__ import annotations

from datetime import UTC, datetime

from pydantic import SecretStr

from canvas_assistant.app import build_health_text
from canvas_assistant.config import IncidentConfig, Settings
from canvas_assistant.incidents import IncidentService


def test_build_health_text_contains_schema_and_integrity(repo, user) -> None:
    settings = Settings(
        canvas_base_url="https://canvas.test",
        canvas_token=SecretStr("secret_token"),
        db_path=repo.db.path,
    )
    now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    incidents = IncidentService(repo, user, None, IncidentConfig())
    text = build_health_text(
        repo,
        settings,
        incidents=incidents,
        started_at=now,
    )

    assert "Saúde do assistente" in text
    assert "schema=" in text
    assert "integrity=" in text
    assert "uptime:" in text
    assert "incidentes ativos:" in text
