from __future__ import annotations

import json
import logging
from typing import Any

from ollama import AsyncClient

from ..config import LLMConfig
from .base import LLMResponse, Message, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class OllamaProvider:
    """Provider local via SDK nativo do Ollama (tools + controle de thinking)."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.client = AsyncClient(host=config.base_url, timeout=config.timeout_seconds)

    def _messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                result.append(
                    {
                        "role": "tool",
                        "tool_name": message.tool_name or "",
                        "content": message.content,
                    }
                )
                continue
            if message.role == "assistant" and message.tool_calls:
                result.append(
                    {
                        "role": "assistant",
                        "content": message.content or "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": call.name,
                                    "arguments": call.arguments,
                                }
                            }
                            for call in message.tool_calls
                        ],
                    }
                )
                continue
            result.append({"role": message.role, "content": message.content})
        return result

    @staticmethod
    def _parse_tool_calls(message: Any) -> list[ToolCall]:
        calls: list[ToolCall] = []
        raw_calls = _get(message, "tool_calls", None) or []
        for raw in raw_calls:
            function = _get(raw, "function", {}) or {}
            name = _get(function, "name")
            if not name:
                continue
            arguments = _get(function, "arguments", {}) or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(ToolCall(name=str(name), arguments=arguments))
        return calls

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._messages(messages),
            "stream": False,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
                "num_ctx": self.config.num_ctx,
            },
        }
        if tools:
            payload["tools"] = [tool.to_openai() for tool in tools]
        if self.config.think is not None:
            payload["think"] = self.config.think

        try:
            response = await self.client.chat(**payload)
        except Exception as exc:
            if "think" in str(exc).casefold() or "unknown field" in str(exc).casefold():
                logger.warning("Modelo sem suporte a 'think'; repetindo sem o parâmetro")
                payload.pop("think", None)
                response = await self.client.chat(**payload)
            else:
                raise

        message = _get(response, "message", response)
        content = _get(message, "content", "") or ""
        return LLMResponse(text=str(content), tool_calls=self._parse_tool_calls(message))

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None) or getattr(self.client, "close", None)
        if close is None:
            return
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except Exception:
            logger.debug("Falha ao fechar cliente Ollama", exc_info=True)
