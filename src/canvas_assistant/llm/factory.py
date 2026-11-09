from __future__ import annotations

from ..config import LLMConfig
from .base import LLMProvider
from .ollama_provider import OllamaProvider
from .openai_compat import OpenAICompatProvider


def create_provider(config: LLMConfig) -> LLMProvider:
    if config.provider == "ollama":
        return OllamaProvider(config)
    if config.provider in ("openai", "grok", "gemini"):
        if not config.api_key:
            raise ValueError(f"LLM_API_KEY é obrigatório para o provider '{config.provider}'")
        return OpenAICompatProvider(config)
    raise ValueError(f"provider de LLM desconhecido: {config.provider}")
