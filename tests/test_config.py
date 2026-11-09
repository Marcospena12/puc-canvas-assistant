from __future__ import annotations

from datetime import time

import pytest

from canvas_assistant.config import Settings


def test_settings_normalizes_canvas_url_and_parses_ids() -> None:
    settings = Settings(
        _env_file=None,
        canvas_base_url="https://canvas.test/api/v1/",
        canvas_token="token",
        telegram_allowed_user_ids="10, 20  30 inválido",
    )

    assert settings.canvas_base_url == "https://canvas.test"
    assert settings.allowed_telegram_user_ids == frozenset({10, 20, 30})
    assert settings.user.canvas_token == "token"


def test_settings_llm_presets() -> None:
    settings = Settings(
        _env_file=None,
        canvas_base_url="https://canvas.test",
        canvas_token="token",
        llm_provider="openai",
        llm_api_key="key",
    )

    config = settings.llm
    assert config.base_url == "https://api.openai.com/v1"
    assert config.api_key == "key"
    assert config.think is False


def test_settings_custom_llm_base_url() -> None:
    settings = Settings(
        _env_file=None,
        canvas_base_url="https://canvas.test",
        canvas_token="token",
        llm_provider="ollama",
        llm_base_url="http://192.168.0.10:11434",
    )

    assert settings.llm.base_url == "http://192.168.0.10:11434"


def test_settings_notification_defaults() -> None:
    settings = Settings(
        _env_file=None,
        canvas_base_url="https://canvas.test",
        canvas_token="token",
    )

    config = settings.notification
    assert config.enabled is True
    assert config.due_hours == (2, 24)
    assert config.digest_time == time(8, 0)
    assert config.quiet_start == time(22, 0)
    assert config.quiet_end == time(7, 0)


def test_settings_notification_custom_values() -> None:
    settings = Settings(
        _env_file=None,
        canvas_base_url="https://canvas.test",
        canvas_token="token",
        notify_due_hours="2, 24, 2",
        notify_digest_time="09:15",
        notify_quiet_start="23:00",
        notify_quiet_end="06:30",
    )

    config = settings.notification
    assert config.due_hours == (2, 24)
    assert config.digest_time == time(9, 15)
    assert config.quiet_start == time(23, 0)
    assert config.quiet_end == time(6, 30)


def test_settings_notification_invalid_values() -> None:
    settings = Settings(
        _env_file=None,
        canvas_base_url="https://canvas.test",
        canvas_token="token",
        notify_due_hours="24, zero",
    )
    with pytest.raises(ValueError):
        _ = settings.notification

    settings = Settings(
        _env_file=None,
        canvas_base_url="https://canvas.test",
        canvas_token="token",
        notify_quiet_start="25:00",
    )
    with pytest.raises(ValueError):
        _ = settings.notification
