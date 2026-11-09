from __future__ import annotations

import json
import logging
from typing import Any

from openai import AsyncOpenAI

from ..config import LLMConfig
from .base import LLMResponse, Message, ToolCall, ToolSpec

logger = logging.getLogger(__name__)


class OpenAICompatProvider:
    """Provider para OpenAI, Grok (xAI) e Gemini via API compatível com OpenAI."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.api_key or "not-needed",
            base_url=config.base_url,
            timeout=config.timeout_seconds,
        )

    @staticmethod
    def _messages(messages: list[Message]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                result.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or message.tool_name or "call_0",
                        "content": message.content,
                    }
                )
                continue
            if message.role == "assistant" and message.tool_calls:
                result.append(
                    {
                        "role": "assistant",
                        "content": message.content or None,
                        "tool_calls": [
                            {
                                "id": call.id or f"call_{index}",
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                                },
                            }
                            for index, call in enumerate(message.tool_calls)
                        ],
                    }
                )
                continue
            result.append({"role": message.role, "content": message.content})
        return result

    @staticmethod
    def _parse_tool_calls(message: Any) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index, raw in enumerate(getattr(message, "tool_calls", None) or []):
            function = getattr(raw, "function", None)
            name = getattr(function, "name", None) if function else None
            if not name:
                continue
            arguments = getattr(function, "arguments", "{}") or "{}"
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(
                ToolCall(
                    name=str(name),
                    arguments=arguments,
                    id=getattr(raw, "id", None) or f"call_{index}",
                )
            )
        return calls

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._messages(messages),
        }
        if tools:
            payload["tools"] = [tool.to_openai() for tool in tools]
        if self.config.provider == "openai":
            payload["max_completion_tokens"] = self.config.max_tokens
        else:
            payload["max_tokens"] = self.config.max_tokens
            payload["temperature"] = self.config.temperature

        response = await self.client.chat.completions.create(**payload)
        message = response.choices[0].message
        content = message.content or ""
        return LLMResponse(text=str(content), tool_calls=self._parse_tool_calls(message))

    async def aclose(self) -> None:
        await self.client.close()
