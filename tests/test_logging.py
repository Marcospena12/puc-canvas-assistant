from __future__ import annotations

import logging

from canvas_assistant.logging_setup import RedactingFormatter, redact_secrets, setup_logging


def test_redact_secrets_masks_sensitive_tokens() -> None:
    cases = [
        ("CANVAS_TOKEN=7~abcdef1234567890", "CANVAS_TOKEN=***REDACTED***"),
        (
            "TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIJKlmNoPQRsTUVwxyZ1234",
            "TELEGRAM_BOT_TOKEN=***REDACTED***",
        ),
        ("ANTHROPIC_API_KEY=sk-ant-api03-abcdef123456", "ANTHROPIC_API_KEY=***REDACTED***"),
        ("Authorization: Bearer my_secret_token_12345", "Authorization: Bearer ***REDACTED***"),
        ("Iniciando sync normal sem segredos.", "Iniciando sync normal sem segredos."),
    ]
    for raw, expected in cases:
        assert redact_secrets(raw) == expected, raw


def test_redacting_formatter_formats_record() -> None:
    formatter = RedactingFormatter(fmt="%(levelname)s: %(message)s")
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=1,
        msg="Conectando com canvas_token=7~abcdef1234567890",
        args=(),
        exc_info=None,
    )
    output = formatter.format(record)
    assert "7~abcdef1234567890" not in output
    assert "canvas_token=***REDACTED***" in output


def test_setup_logging_writes_to_rotating_file(tmp_path) -> None:
    log_file = tmp_path / "test.log"
    setup_logging(level="INFO", log_file=log_file)
    logger = logging.getLogger("test_rot")
    logger.info("Teste com token=abcdef1234567890")

    content = log_file.read_text(encoding="utf-8")
    assert "token=***REDACTED***" in content
    assert "abcdef1234567890" not in content
