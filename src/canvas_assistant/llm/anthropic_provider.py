from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from ..config import LLMConfig
from .base import LLMResponse, Message, ToolCall, ToolSpec

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_RETRY_STATUS = {429, 500, 502, 503, 504}


class AnthropicProvider:
    """Provider nativo para a API da Anthropic (Claude) via httpx.

    Suporta tool use (ferramentas), retry com backoff em 429/5xx e timeout.
    """

    def __init__(self, config: LLMConfig) -> None:
        self.config = config
        self.client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/"),
            timeout=config.timeout_seconds,
            headers={
                "x-api-key": config.api_key or "",
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    def _build_messages(self, messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
        system_content = ""
        anthropic_messages: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                system_content = f"{system_content}\n\n{m.content}".strip()
            elif m.role in ("user", "assistant"):
                if m.tool_calls:
                    content: list[dict[str, Any]] = []
                    if m.content:
                        content.append({"type": "text", "text": m.content})
                    for call in m.tool_calls:
                        content.append(
                            {
                                "type": "tool_use",
                                "id": call.id or "",
                                "name": call.name,
                                "input": call.arguments,
                            }
                        )
                    anthropic_messages.append({"role": m.role, "content": content})
                else:
                    anthropic_messages.append({"role": m.role, "content": m.content})
            elif m.role == "tool":
                anthropic_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": m.tool_call_id or "",
                                "content": m.content,
                            }
                        ],
                    }
                )
        if not anthropic_messages:
            anthropic_messages = [{"role": "user", "content": "Olá"}]
        return system_content, anthropic_messages

    def _build_tools(self, tools: list[ToolSpec]) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in tools
        ]

    @staticmethod
    def _parse_tool_calls(content_blocks: list[dict[str, Any]]) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for block in content_blocks:
            if block.get("type") != "tool_use":
                continue
            raw_input = block.get("input", {})
            if isinstance(raw_input, str):
                try:
                    raw_input = json.loads(raw_input)
                except json.JSONDecodeError:
                    raw_input = {}
            if not isinstance(raw_input, dict):
                raw_input = {}
            calls.append(
                ToolCall(
                    name=str(block.get("name") or ""),
                    arguments=raw_input,
                    id=str(block.get("id") or ""),
                )
            )
        return calls

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        system_content, anthropic_messages = self._build_messages(messages)
        payload: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "messages": anthropic_messages,
        }
        if system_content:
            payload["system"] = system_content
        if tools:
            payload["tools"] = self._build_tools(tools)

        prompt_chars = sum(len(m.content) for m in messages)
        est_in_tok = max(1, prompt_chars // 4)
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                resp = await self.client.post("/messages", json=payload)
                if resp.status_code in _RETRY_STATUS and attempt < _MAX_RETRIES - 1:
                    delay = 0.5 * (2**attempt)
                    logger.warning(
                        "Anthropic retornou %s; tentando novamente em %.1fs (tentativa %d/%d)",
                        resp.status_code,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                content_blocks = data.get("content", [])
                text_parts = [
                    block["text"] for block in content_blocks if block.get("type") == "text"
                ]
                text_content = "\n".join(text_parts)
                tool_calls = self._parse_tool_calls(content_blocks)
                usage = data.get("usage", {})
                input_tokens = int(usage.get("input_tokens", 0))
                output_tokens = int(usage.get("output_tokens", 0))
                logger.info(
                    "Anthropic: prompt_chars=%s est_in_tok=%s in_tok=%s out_tok=%s tool_calls=%d",
                    prompt_chars,
                    est_in_tok,
                    input_tokens,
                    output_tokens,
                    len(tool_calls),
                )
                return LLMResponse(
                    text=text_content,
                    tool_calls=tool_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES - 1:
                    logger.warning(
                        "Anthropic timeout; tentando novamente (tentativa %d/%d)",
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(0.5 * (2**attempt))
                    continue
                raise
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                status = exc.response.status_code
                if status in _RETRY_STATUS and attempt < _MAX_RETRIES - 1:
                    delay = 0.5 * (2**attempt)
                    logger.warning(
                        "Anthropic retornou %s; tentando novamente em %.1fs (tentativa %d/%d)",
                        status,
                        delay,
                        attempt + 1,
                        _MAX_RETRIES,
                    )
                    time.sleep(delay)
                    continue
                raise
        raise last_exc if last_exc else RuntimeError("Anthropic: falha desconhecida")

    async def aclose(self) -> None:
        await self.client.aclose()
