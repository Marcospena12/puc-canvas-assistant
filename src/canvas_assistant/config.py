from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class UserProfile:
    user_id: str
    canvas_base_url: str
    canvas_token: str
    telegram_chat_id: int | None
    telegram_user_ids: frozenset[int]


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

    db_path: Path = Path("data/assistant.sqlite3")
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
