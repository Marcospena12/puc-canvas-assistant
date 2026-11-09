from __future__ import annotations

import json
from datetime import timedelta

from canvas_assistant.agent.agent import FALLBACK_ANSWER, Agent
from canvas_assistant.agent.tools import ToolRegistry
from canvas_assistant.llm.base import LLMResponse, Message, ToolCall, ToolSpec
from canvas_assistant.timeutil import to_utc_iso, utc_now


class FakeProvider:
    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[Message], list[ToolSpec]]] = []
        self.closed = False

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append((messages, tools))
        if not self.responses:
            raise RuntimeError("sem respostas preparadas")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self) -> None:
        self.closed = True


def seed(repo, user, course, assignment) -> None:
    repo.upsert_courses(user.user_id, [course])
    repo.replace_assignments(user.user_id, course.id, [assignment])


async def test_agent_executes_tool_and_returns_answer(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    course = course_factory()
    assignment = assignment_factory(due_at=to_utc_iso(utc_now() + timedelta(days=1)))
    seed(repo, user, course, assignment)
    provider = FakeProvider(
        [
            LLMResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        name="get_upcoming_items",
                        arguments={"period": "next_7_days"},
                        id="call-1",
                    )
                ],
            ),
            LLMResponse(text="Você tem uma tarefa esta semana."),
        ]
    )
    agent = Agent(provider, repo, user, tz)

    answer = await agent.reply("o que tenho essa semana?")

    assert answer == "Você tem uma tarefa esta semana."
    assert len(provider.calls) == 2
    first_messages, first_tools = provider.calls[0]
    assert first_messages[0].role == "system"
    assert "Cálculo I" in first_messages[0].content
    assert {spec.name for spec in first_tools} >= {"get_upcoming_items", "get_grades"}

    second_messages, _ = provider.calls[1]
    tool_messages = [message for message in second_messages if message.role == "tool"]
    assert len(tool_messages) == 1
    result = json.loads(tool_messages[0].content)
    assert result["ok"] is True
    assert result["items"][0]["title"] == "Lista 1"
    assert tool_messages[0].tool_call_id == "call-1"


async def test_agent_returns_sync_hint_when_cache_is_empty(repo, user, tz) -> None:
    provider = FakeProvider([])
    agent = Agent(provider, repo, user, tz)

    answer = await agent.reply("o que tenho hoje?")

    assert "Rode /sync" in answer
    assert provider.calls == []


async def test_agent_returns_fallback_on_provider_error(repo, user, tz, course_factory) -> None:
    repo.upsert_courses(user.user_id, [course_factory()])
    provider = FakeProvider([RuntimeError("ollama offline")])
    agent = Agent(provider, repo, user, tz)

    answer = await agent.reply("notas?")

    assert answer == FALLBACK_ANSWER


async def test_agent_stops_after_max_tool_rounds(repo, user, tz, course_factory) -> None:
    repo.upsert_courses(user.user_id, [course_factory()])
    loop_response = LLMResponse(
        text="",
        tool_calls=[ToolCall(name="get_courses", arguments={}, id="loop")],
    )
    provider = FakeProvider([loop_response, loop_response, loop_response])
    agent = Agent(provider, repo, user, tz, max_tool_rounds=2)

    answer = await agent.reply("loop")

    assert answer == FALLBACK_ANSWER
    assert len(provider.calls) == 2


async def test_agent_reports_unknown_tool_to_model(repo, user, tz, course_factory) -> None:
    repo.upsert_courses(user.user_id, [course_factory()])
    provider = FakeProvider(
        [
            LLMResponse(
                text="",
                tool_calls=[ToolCall(name="does_not_exist", arguments={}, id="x")],
            ),
            LLMResponse(text="Não tenho essa ferramenta."),
        ]
    )
    agent = Agent(provider, repo, user, tz)

    answer = await agent.reply("teste")

    assert answer == "Não tenho essa ferramenta."
    tool_message = next(message for message in provider.calls[1][0] if message.role == "tool")
    assert "ferramenta desconhecida" in tool_message.content


async def test_agent_accepts_link_from_current_context(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    course = course_factory()
    assignment = assignment_factory(due_at=to_utc_iso(utc_now() + timedelta(days=1)))
    seed(repo, user, course, assignment)
    provider = FakeProvider(
        [LLMResponse(text=("Você tem [Lista 1](https://canvas.test/assignments/100) para amanhã."))]
    )
    agent = Agent(provider, repo, user, tz)

    answer = await agent.reply("o que tenho amanhã?")

    assert "Lista 1" in answer
    assert len(provider.calls) == 1


async def test_agent_corrects_invented_task_and_falls_back(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    course = course_factory(id=292828, name="Trabalho Interdisciplinar")
    assignment = assignment_factory(
        id=1404750,
        course_id=292828,
        name="Elaboração de projeto (passo 2)",
        html_url="https://canvas.test/courses/292828/assignments/1404750",
        due_at=to_utc_iso(utc_now() + timedelta(days=1)),
    )
    seed(repo, user, course, assignment)
    fake = "https://canvas.test/courses/292828/assignments/1404751"
    provider = FakeProvider(
        [
            LLMResponse(
                text="",
                tool_calls=[
                    ToolCall(
                        name="get_upcoming_items",
                        arguments={"period": "next_7_days"},
                        id="call-1",
                    )
                ],
            ),
            LLMResponse(
                text=(
                    f"Você tem 2 tarefas: Elaboração (passo 2) e [Relatório de atividades]({fake})."
                )
            ),
            LLMResponse(
                text=(
                    f"Você tem 2 tarefas: Elaboração (passo 2) e [Relatório de atividades]({fake})."
                )
            ),
        ]
    )
    agent = Agent(provider, repo, user, tz)

    answer = await agent.reply("o que tenho essa semana?")

    assert len(provider.calls) == 3
    correction_messages = [
        message
        for message in provider.calls[2][0]
        if message.role == "system" and "não confirmados" in message.content
    ]
    assert correction_messages
    assert "Elaboração de projeto (passo 2)" in answer
    assert "1404750" in answer
    assert "1404751" not in answer
    assert "Relatório de atividades" not in answer


async def test_tool_registry_rejects_invalid_arguments(repo, user, tz) -> None:
    registry = ToolRegistry(repo, user, tz)

    result = json.loads(await registry.execute("get_upcoming_items", {"period": "someday"}))

    assert result["ok"] is False
    assert result["error"] == "argumentos inválidos"
