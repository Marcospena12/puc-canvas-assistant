"""Testes do provider Anthropic (A6): tool use, retry 429/5xx, timeout.

Valida o pipeline de fundamentação com HTTP mockado (respx).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from canvas_assistant.config import LLMConfig
from canvas_assistant.llm.anthropic_provider import AnthropicProvider
from canvas_assistant.llm.base import Message, ToolCall, ToolSpec


def _make_config(**overrides) -> LLMConfig:
    defaults = dict(
        provider="anthropic",
        model="claude-sonnet-5-5",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-test",
        think=False,
        temperature=0.2,
        max_tokens=800,
        num_ctx=16384,
        timeout_seconds=30,
        keep_alive="30m",
    )
    defaults.update(overrides)
    return LLMConfig(**defaults)


def _make_tool() -> ToolSpec:
    return ToolSpec(
        name="get_grades",
        description="Obtém notas do aluno",
        input_schema={"type": "object", "properties": {"course_id": {"type": "integer"}}},
    )


class TestAnthropicProviderChat:
    """A6: chat simples sem tools."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_chat_returns_text_and_tokens(self) -> None:
        config = _make_config()
        provider = AnthropicProvider(config)
        respx.post("https://api.anthropic.com/v1/messages").respond(
            200,
            json={
                "content": [{"type": "text", "text": "Olá, aluno!"}],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )
        resp = await provider.chat([Message(role="user", content="oi")], tools=[])
        assert resp.text == "Olá, aluno!"
        assert resp.input_tokens == 10
        assert resp.output_tokens == 5
        assert resp.tool_calls == []
        await provider.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_chat_handles_system_prompt(self) -> None:
        config = _make_config()
        provider = AnthropicProvider(config)
        respx.post("https://api.anthropic.com/v1/messages").respond(
            200,
            json={
                "content": [{"type": "text", "text": "ok"}],
                "usage": {"input_tokens": 20, "output_tokens": 3},
            },
        )
        resp = await provider.chat(
            [
                Message(role="system", content="Você é um tutor."),
                Message(role="user", content="oi"),
            ],
            tools=[],
        )
        assert resp.text == "ok"
        await provider.aclose()


class TestAnthropicProviderToolUse:
    """A6: tool use — provider envia tools e parseia tool_calls."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_chat_parses_tool_use(self) -> None:
        config = _make_config()
        provider = AnthropicProvider(config)
        respx.post("https://api.anthropic.com/v1/messages").respond(
            200,
            json={
                "content": [
                    {"type": "text", "text": "Vou consultar suas notas."},
                    {
                        "type": "tool_use",
                        "id": "tool_1",
                        "name": "get_grades",
                        "input": {"course_id": 1},
                    },
                ],
                "usage": {"input_tokens": 50, "output_tokens": 30},
            },
        )
        resp = await provider.chat(
            [Message(role="user", content="qual minha média?")],
            tools=[_make_tool()],
        )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "get_grades"
        assert resp.tool_calls[0].arguments == {"course_id": 1}
        assert resp.tool_calls[0].id == "tool_1"
        await provider.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_chat_sends_tools_in_payload(self) -> None:
        config = _make_config()
        provider = AnthropicProvider(config)
        captured: dict = {}

        def _capture(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "ok"}],
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                },
            )

        respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_capture)
        await provider.chat([Message(role="user", content="oi")], tools=[_make_tool()])
        assert "tools" in captured["body"]
        assert captured["body"]["tools"][0]["name"] == "get_grades"
        await provider.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_chat_handles_tool_result_message(self) -> None:
        """Provider converte Message(role='tool') para tool_result."""
        config = _make_config()
        provider = AnthropicProvider(config)
        captured: dict = {}

        def _capture(request):
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "resposta final"}],
                    "usage": {"input_tokens": 60, "output_tokens": 10},
                },
            )

        respx.post("https://api.anthropic.com/v1/messages").mock(side_effect=_capture)
        await provider.chat(
            [
                Message(role="user", content="qual minha média?"),
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(name="get_grades", arguments={}, id="tool_1")],
                ),
                Message(
                    role="tool",
                    content='{"media": 8.5}',
                    tool_call_id="tool_1",
                    tool_name="get_grades",
                ),
            ],
            tools=[_make_tool()],
        )
        messages = captured["body"]["messages"]
        # tool_result vira role=user com content[0].type=tool_result
        found_tool_result = False
        for m in messages:
            content = m.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        found_tool_result = True
        assert found_tool_result
        await provider.aclose()


class TestAnthropicProviderRetry:
    """A6: retry em 429/5xx com backoff."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_retry_on_429_then_succeeds(self, monkeypatch) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        config = _make_config()
        provider = AnthropicProvider(config)
        route = respx.post("https://api.anthropic.com/v1/messages")
        route.mock(
            side_effect=[
                httpx.Response(429, json={"error": "rate_limit"}),
                httpx.Response(
                    200,
                    json={
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 5, "output_tokens": 2},
                    },
                ),
            ]
        )
        resp = await provider.chat([Message(role="user", content="oi")], tools=[])
        assert resp.text == "ok"
        await provider.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_retry_on_500_then_succeeds(self, monkeypatch) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        config = _make_config()
        provider = AnthropicProvider(config)
        route = respx.post("https://api.anthropic.com/v1/messages")
        route.mock(
            side_effect=[
                httpx.Response(503, json={"error": "unavailable"}),
                httpx.Response(
                    200,
                    json={
                        "content": [{"type": "text", "text": "ok"}],
                        "usage": {"input_tokens": 5, "output_tokens": 2},
                    },
                ),
            ]
        )
        resp = await provider.chat([Message(role="user", content="oi")], tools=[])
        assert resp.text == "ok"
        await provider.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_raises_after_max_retries(self, monkeypatch) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        config = _make_config(timeout_seconds=10)
        provider = AnthropicProvider(config)
        respx.post("https://api.anthropic.com/v1/messages").respond(
            429, json={"error": "rate_limit"}
        )
        with pytest.raises((httpx.HTTPStatusError, Exception)):
            await provider.chat([Message(role="user", content="oi")], tools=[])
        await provider.aclose()


class TestAnthropicProviderTimeout:
    """A6: timeout propagado."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_raises(self, monkeypatch) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        config = _make_config(timeout_seconds=1)
        provider = AnthropicProvider(config)
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=httpx.TimeoutException("timeout")
        )
        with pytest.raises(httpx.TimeoutException):
            await provider.chat([Message(role="user", content="oi")], tools=[])
        await provider.aclose()


class TestAnthropicProviderGrounding:
    """A6: pipeline de fundamentação funciona igual (tool use → resposta)."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_tool_roundtrip_produces_grounded_response(self, monkeypatch) -> None:
        monkeypatch.setattr("time.sleep", lambda _: None)
        config = _make_config()
        provider = AnthropicProvider(config)
        # Primeiro: LLM pede tool_use
        # Segundo: LLM responde com texto fundamentado
        route = respx.post("https://api.anthropic.com/v1/messages")
        route.mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        "content": [
                            {"type": "text", "text": "Vou verificar."},
                            {
                                "type": "tool_use",
                                "id": "t1",
                                "name": "get_grades",
                                "input": {"course_id": 1},
                            },
                        ],
                        "usage": {"input_tokens": 30, "output_tokens": 20},
                    },
                ),
                httpx.Response(
                    200,
                    json={
                        "content": [{"type": "text", "text": "Sua média é 8.5 [F1]."}],
                        "usage": {"input_tokens": 60, "output_tokens": 10},
                    },
                ),
            ]
        )
        # Round 1: LLM pede tool
        resp1 = await provider.chat(
            [Message(role="user", content="qual minha média em BI?")],
            tools=[_make_tool()],
        )
        assert len(resp1.tool_calls) == 1
        # Round 2: LLM responde com texto
        resp2 = await provider.chat(
            [
                Message(role="user", content="qual minha média em BI?"),
                Message(role="assistant", content="Vou verificar.", tool_calls=resp1.tool_calls),
                Message(
                    role="tool",
                    content='{"media": 8.5}',
                    tool_call_id="t1",
                    tool_name="get_grades",
                ),
            ],
            tools=[_make_tool()],
        )
        assert "8.5" in resp2.text
        assert resp2.tool_calls == []
        await provider.aclose()
