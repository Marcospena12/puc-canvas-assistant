from __future__ import annotations

from ..config import LLMConfig, Settings
from .anthropic_provider import AnthropicProvider
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
    if config.provider == "anthropic":
        if not config.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY ou LLM_API_KEY é obrigatório para o provider 'anthropic'"
            )
        return AnthropicProvider(config)
    raise ValueError(f"provider de LLM desconhecido: {config.provider}")


def create_study_provider(settings: Settings) -> LLMProvider:
    base_llm = settings.llm
    study_cfg = settings.study
    api_key = (
        settings.anthropic_api_key.get_secret_value()
        if settings.anthropic_api_key and base_llm.provider == "anthropic"
        else base_llm.api_key
    )
    study_llm = LLMConfig(
        provider=base_llm.provider,
        model=study_cfg.model,
        base_url=base_llm.base_url,
        api_key=api_key,
        think=base_llm.think,
        temperature=base_llm.temperature,
        max_tokens=study_cfg.max_output_tokens,
        num_ctx=study_cfg.num_ctx,
        timeout_seconds=base_llm.timeout_seconds,
        keep_alive=base_llm.keep_alive,
    )
    return create_provider(study_llm)
