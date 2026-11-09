from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProviderName = Literal["ollama", "openai", "grok", "gemini"]

_DEFAULT_BASE_URLS: dict[str, str] = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "grok": "https://api.x.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
}


def _parse_clock(value: str, field: str) -> time:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", (value or "").strip())
    if not match:
        raise ValueError(f"{field} deve estar no formato HH:MM")
    hour, minute = int(match.group(1)), int(match.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{field} deve ser um horário válido")
    return time(hour, minute)


def _parse_file_types(value: str) -> frozenset[str]:
    types = {
        part.strip().lower().lstrip(".")
        for part in re.split(r"[,\s]+", (value or "").strip())
        if part.strip()
    }
    if not types:
        raise ValueError("FILE_ALLOWED_TYPES não pode ficar vazio")
    return frozenset(types)


def _parse_due_hours(value: str) -> tuple[int, ...]:
    hours: set[int] = set()
    for part in re.split(r"[,\s]+", (value or "").strip()):
        if not part:
            continue
        try:
            hour = int(part)
        except ValueError as exc:
            raise ValueError("NOTIFY_DUE_HOURS deve conter horas inteiras") from exc
        if hour <= 0:
            raise ValueError("NOTIFY_DUE_HOURS deve conter apenas horas positivas")
        hours.add(hour)
    if not hours:
        raise ValueError("NOTIFY_DUE_HOURS não pode ficar vazio")
    return tuple(sorted(hours))


@dataclass(frozen=True, slots=True)
class UserProfile:
    user_id: str
    canvas_base_url: str
    canvas_token: str
    telegram_chat_id: int | None
    telegram_user_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    enabled: bool
    new_assignments: bool
    due_hours: tuple[int, ...]
    announcements: bool
    new_files: bool
    digest: bool
    digest_time: time
    quiet_start: time
    quiet_end: time
    max_stale_minutes: int
    digest_grace_minutes: int


@dataclass(frozen=True, slots=True)
class FileSyncConfig:
    enabled: bool
    interval_minutes: int
    files_dir: Path
    max_size_mb: int
    allowed_types: frozenset[str]
    blocked_retry_hours: int
    download_retry_hours: int
    download_timeout_seconds: int

    @property
    def max_size_bytes(self) -> int:
        return self.max_size_mb * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LLMConfig:
    provider: LLMProviderName
    model: str
    base_url: str
    api_key: str | None
    think: bool
    temperature: float
    max_tokens: int
    num_ctx: int
    timeout_seconds: int


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    canvas_base_url: str
    canvas_token: SecretStr

    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: int | None = None
    telegram_allowed_user_ids: str = ""

    llm_provider: LLMProviderName = "ollama"
    llm_model: str = "qwen3:14b"
    llm_base_url: str = ""
    llm_api_key: SecretStr | None = None
    llm_think: bool = False
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=800, ge=64, le=8192)
    llm_num_ctx: int = Field(default=8192, ge=2048, le=131072)
    llm_timeout_seconds: int = Field(default=180, ge=10, le=900)

    notify_enabled: bool = True
    notify_new_assignments: bool = True
    notify_due_hours: str = "24,2"
    notify_announcements: bool = True
    notify_new_files: bool = True
    notify_digest: bool = True
    notify_digest_time: str = "08:00"
    notify_quiet_start: str = "22:00"
    notify_quiet_end: str = "07:00"
    notify_max_stale_minutes: int = Field(default=45, ge=5, le=1440)
    notify_check_minutes: int = Field(default=5, ge=1, le=60)
    notify_digest_grace_minutes: int = Field(default=120, ge=0, le=720)

    db_path: Path = Path("data/assistant.sqlite3")
    file_sync_enabled: bool = True
    file_sync_interval_minutes: int = Field(default=60, ge=5, le=1440)
    files_dir: Path = Path("data/files")
    file_max_size_mb: int = Field(default=50, ge=1, le=1024)
    file_allowed_types: str = "pdf,pptx,docx,txt,md"
    file_blocked_retry_hours: int = Field(default=24, ge=1, le=168)
    file_download_retry_hours: int = Field(default=6, ge=1, le=168)
    file_download_timeout_seconds: int = Field(default=120, ge=10, le=900)
    timezone: str = "America/Sao_Paulo"
    poll_interval_minutes: int = Field(default=20, ge=5, le=240)
    sync_past_days: int = Field(default=7, ge=0, le=60)
    sync_future_days: int = Field(default=30, ge=1, le=365)
    announcements_lookback_days: int = Field(default=60, ge=1, le=365)
    log_level: str = "INFO"
    user_id: str = "default"

    @field_validator("canvas_base_url")
    @classmethod
    def _normalize_canvas_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if value.endswith("/api/v1"):
            value = value[: -len("/api/v1")]
        if not value.startswith(("http://", "https://")):
            raise ValueError("CANVAS_BASE_URL deve começar com http:// ou https://")
        return value

    @property
    def allowed_telegram_user_ids(self) -> frozenset[int]:
        raw = re.split(r"[,\s]+", self.telegram_allowed_user_ids.strip())
        ids: set[int] = set()
        for part in raw:
            if not part:
                continue
            try:
                ids.add(int(part))
            except ValueError:
                continue
        return frozenset(ids)

    @property
    def user(self) -> UserProfile:
        return UserProfile(
            user_id=self.user_id,
            canvas_base_url=self.canvas_base_url,
            canvas_token=self.canvas_token.get_secret_value(),
            telegram_chat_id=self.telegram_chat_id,
            telegram_user_ids=self.allowed_telegram_user_ids,
        )

    @property
    def notification(self) -> NotificationConfig:
        return NotificationConfig(
            enabled=self.notify_enabled,
            new_assignments=self.notify_new_assignments,
            due_hours=_parse_due_hours(self.notify_due_hours),
            announcements=self.notify_announcements,
            new_files=self.notify_new_files,
            digest=self.notify_digest,
            digest_time=_parse_clock(self.notify_digest_time, "NOTIFY_DIGEST_TIME"),
            quiet_start=_parse_clock(self.notify_quiet_start, "NOTIFY_QUIET_START"),
            quiet_end=_parse_clock(self.notify_quiet_end, "NOTIFY_QUIET_END"),
            max_stale_minutes=self.notify_max_stale_minutes,
            digest_grace_minutes=self.notify_digest_grace_minutes,
        )

    @property
    def file_sync(self) -> FileSyncConfig:
        return FileSyncConfig(
            enabled=self.file_sync_enabled,
            interval_minutes=self.file_sync_interval_minutes,
            files_dir=self.files_dir,
            max_size_mb=self.file_max_size_mb,
            allowed_types=_parse_file_types(self.file_allowed_types),
            blocked_retry_hours=self.file_blocked_retry_hours,
            download_retry_hours=self.file_download_retry_hours,
            download_timeout_seconds=self.file_download_timeout_seconds,
        )

    @property
    def llm(self) -> LLMConfig:
        base_url = (self.llm_base_url or "").strip() or _DEFAULT_BASE_URLS[self.llm_provider]
        api_key = self.llm_api_key.get_secret_value() if self.llm_api_key else None
        return LLMConfig(
            provider=self.llm_provider,
            model=self.llm_model,
            base_url=base_url,
            api_key=api_key,
            think=self.llm_think,
            temperature=self.llm_temperature,
            max_tokens=self.llm_max_tokens,
            num_ctx=self.llm_num_ctx,
            timeout_seconds=self.llm_timeout_seconds,
        )
