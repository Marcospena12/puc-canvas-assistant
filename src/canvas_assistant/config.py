from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProviderName = Literal["ollama", "openai", "grok", "gemini", "anthropic"]

_DEFAULT_BASE_URLS: dict[str, str] = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "grok": "https://api.x.ai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "anthropic": "https://api.anthropic.com/v1",
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
    new_pages: bool = True


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
class IncidentConfig:
    enabled: bool = True
    canvas_down_minutes: int = 10
    llm_failures: int = 3


@dataclass(frozen=True, slots=True)
class StudyConfig:
    model: str
    top_k: int
    context_max_tokens: int
    max_output_tokens: int
    summary_max_chunks: int
    grounding_min_overlap: float
    num_ctx: int


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
    keep_alive: str = "30m"


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
    agent_model: str = ""
    llm_base_url: str = ""
    llm_api_key: SecretStr | None = None
    llm_think: bool = False
    llm_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=800, ge=64, le=8192)
    llm_num_ctx: int = Field(default=16384, ge=2048, le=131072)
    llm_timeout_seconds: int = Field(default=180, ge=10, le=900)
    llm_keep_alive: str = "30m"

    study_model: str = ""
    study_top_k: int = Field(default=8, ge=1, le=20)
    study_context_max_tokens: int = Field(default=6000, ge=1000, le=32000)
    study_max_output_tokens: int = Field(default=1200, ge=200, le=8192)
    study_summary_max_chunks: int = Field(default=40, ge=10, le=200)
    study_grounding_min_overlap: float = Field(default=0.35, ge=0.0, le=1.0)
    anthropic_api_key: SecretStr | None = None

    notify_enabled: bool = True
    notify_new_assignments: bool = True

    # Catch-up ao iniciar (Etapa A 4.1.2): detecta lacuna > CATCHUP_GAP_MINUTES
    # desde o último ciclo do scheduler e envia uma mensagem consolidada.
    catchup_gap_minutes: int = Field(default=30, ge=1, le=1440)
    catchup_urgent_hours: int = Field(default=6, ge=1, le=168)

    # Heartbeat externo (Etapa A 4.1.2): GET opcional a cada HEARTBEAT_INTERVAL_MINUTES
    # somente se o último ciclo do scheduler foi bem-sucedido.
    heartbeat_url: str = ""
    heartbeat_interval_minutes: int = Field(default=10, ge=1, le=1440)

    notify_new_assignments: bool = True
    notify_due_hours: str = "24,2"
    notify_announcements: bool = True
    notify_new_files: bool = True
    notify_new_pages: bool = True
    notify_digest: bool = True
    notify_digest_time: str = "08:00"
    notify_quiet_start: str = "22:00"
    notify_quiet_end: str = "07:00"
    notify_max_stale_minutes: int = Field(default=45, ge=5, le=1440)
    notify_check_minutes: int = Field(default=5, ge=1, le=60)
    notify_digest_grace_minutes: int = Field(default=120, ge=0, le=720)
    incident_enabled: bool = True
    incident_canvas_down_minutes: int = Field(default=10, ge=1, le=1440)
    incident_llm_failures: int = Field(default=3, ge=1, le=20)

    # Revisão de rascunho pelo tutor (Etapa B 4.2)
    review_session_minutes: int = Field(default=30, ge=5, le=120)
    review_max_file_mb: int = Field(default=10, ge=1, le=50)
    review_max_chars: int = Field(default=20000, ge=1000, le=100000)
    review_persist_drafts: bool = False
    review_max_text_chars: int = Field(default=4000, ge=100, le=10000)

    db_path: Path = Path("data/assistant.sqlite3")
    content_sync_enabled: bool = True
    content_sync_interval_minutes: int = Field(default=60, ge=5, le=1440)
    content_blocked_retry_hours: int = Field(default=24, ge=1, le=168)
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
            new_pages=self.notify_new_pages,
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
        required_ctx = self.study_context_max_tokens + self.study_max_output_tokens + 2048
        effective_num_ctx = max(self.llm_num_ctx, required_ctx)
        return LLMConfig(
            provider=self.llm_provider,
            model=self.agent_model.strip() or self.llm_model,
            base_url=base_url,
            api_key=api_key,
            think=self.llm_think,
            temperature=self.llm_temperature,
            max_tokens=self.llm_max_tokens,
            num_ctx=effective_num_ctx,
            timeout_seconds=self.llm_timeout_seconds,
            keep_alive=self.llm_keep_alive,
        )

    @property
    def incidents(self) -> IncidentConfig:
        return IncidentConfig(
            enabled=self.incident_enabled,
            canvas_down_minutes=self.incident_canvas_down_minutes,
            llm_failures=self.incident_llm_failures,
        )

    @property
    def study(self) -> StudyConfig:
        required_ctx = self.study_context_max_tokens + self.study_max_output_tokens + 2048
        effective_ctx = max(self.llm_num_ctx, required_ctx)
        return StudyConfig(
            model=self.study_model.strip() or self.llm_model,
            top_k=self.study_top_k,
            context_max_tokens=self.study_context_max_tokens,
            max_output_tokens=self.study_max_output_tokens,
            summary_max_chunks=self.study_summary_max_chunks,
            grounding_min_overlap=self.study_grounding_min_overlap,
            num_ctx=effective_ctx,
        )
