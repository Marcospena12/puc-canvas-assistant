from unittest.mock import AsyncMock

import pytest

from canvas_assistant.config import Settings
from canvas_assistant.knowledge.task_briefing import (
    _cut_at_word_boundary,
    _days_remaining_label,
    _translate_submission_types,
    format_refusal_message,
    generate_task_briefing,
    is_quiz_task,
    is_resolution_request,
    list_pending_tasks_text,
    resolve_task_selection,
)
from canvas_assistant.llm.base import LLMResponse
from canvas_assistant.sync import extract_rubric_from_description, normalize_rubric


def test_rubric_parsing_api_and_description() -> None:
    # 1. Rubrica estruturada via API
    api_raw = [
        {
            "id": "1",
            "points": 5.0,
            "description": "Qualidade do código",
            "long_description": "Código limpo e bem documentado",
        },
        {
            "id": "2",
            "points": 2.5,
            "description": "Testes unitários",
            "long_description": "",
        },
    ]
    parsed_api = normalize_rubric(api_raw, None)
    assert len(parsed_api) == 2
    assert parsed_api[0]["criterion"] == "Qualidade do código"
    assert parsed_api[0]["points"] == 5.0
    assert parsed_api[0]["description"] == "Código limpo e bem documentado"
    assert parsed_api[1]["points"] == 2.5

    # 2. Rubrica extraída da descrição
    desc_html = (
        "<p>Orientações do trabalho.</p>"
        "<p>Critérios de Avaliação (Rubrica):</p>"
        "<ul>"
        "<li>Introdução e objetivos (2,0 pts)</li>"
        "<li>Metodologia aplicada (5,0 pts)</li>"
        "<li>Resultados e discussão (3 pts)</li>"
        "</ul>"
    )
    parsed_desc = extract_rubric_from_description(desc_html)
    assert len(parsed_desc) == 3
    assert parsed_desc[0]["criterion"] == "Introdução e objetivos"
    assert parsed_desc[0]["points"] == 2.0
    assert parsed_desc[1]["criterion"] == "Metodologia aplicada"
    assert parsed_desc[1]["points"] == 5.0
    assert parsed_desc[2]["criterion"] == "Resultados e discussão"
    assert parsed_desc[2]["points"] == 3.0


def test_list_pending_tasks_filters_and_orders(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    c1 = course_factory(id=10, name="Engenharia de Software")
    repo.upsert_courses(user.user_id, [c1])

    # Tarefa entregue (não deve aparecer)
    a_done = assignment_factory(
        id=1, course_id=10, name="Entregue", submitted_at="2026-11-01T10:00:00Z"
    )
    # Tarefa pendente com prazo próximo
    a_soon = assignment_factory(
        id=2, course_id=10, name="Pendente Logo", due_at="2026-11-15T23:59:00Z"
    )
    # Tarefa pendente com prazo distante
    a_late = assignment_factory(
        id=3, course_id=10, name="Pendente Depois", due_at="2026-11-25T23:59:00Z"
    )
    # Tarefa dispensada (excused)
    a_excused = assignment_factory(id=4, course_id=10, name="Dispensada", excused=True)

    repo.replace_assignments(user.user_id, 10, [a_done, a_soon, a_late, a_excused])

    text = list_pending_tasks_text(repo, user.user_id, tz)
    assert "Pendente Logo" in text
    assert "Pendente Depois" in text
    assert "Entregue" not in text
    assert "Dispensada" not in text

    # Ordem por prazo
    pos_soon = text.find("Pendente Logo")
    pos_late = text.find("Pendente Depois")
    assert pos_soon < pos_late


@pytest.mark.asyncio
async def test_briefing_contains_sections_and_no_deliverable(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    c1 = course_factory(id=10, name="Sistemas Distribuídos")
    repo.upsert_courses(user.user_id, [c1])

    rubric_data = [{"criterion": "Arquitetura", "points": 10.0, "description": "Uso de RPC"}]
    a = assignment_factory(
        id=20,
        course_id=10,
        name="Trabalho Prático 1",
        description_html="<p>Implemente um servidor de nomes.</p>",
        rubric=rubric_data,
        due_at="2026-11-20T23:59:00Z",
        points_possible=10.0,
    )
    repo.replace_assignments(user.user_id, 10, [a])

    detail = repo.get_assignment_detail(user.user_id, 20)
    assert detail is not None

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = LLMResponse(
        text=(
            "1. O que pedem:\nImplementação de servidor de nomes.\n\n"
            "5. Plano de trabalho sugerido:\n• Passo 1: Leitura (1h)\n\n"
            "6. Perguntas para se guiar:\n• O servidor responde a consultas?"
        ),
        input_tokens=100,
        output_tokens=50,
    )

    settings = Settings()
    res = await generate_task_briefing(repo, fake_provider, user, detail, tz, settings=settings)

    assert "1. O que pedem" in res.text
    assert "2. Prazo e entrega" in res.text
    assert "3. Checklist da rubrica" in res.text
    assert "Arquitetura" in res.text
    assert "4. Material do acervo relacionado" in res.text
    assert "5. Plano de trabalho sugerido (sugestão)" in res.text
    assert "6. Perguntas para se guiar" in res.text
    assert "O bot não produz respostas entregáveis nem redige trabalhos" in res.text
    assert fake_provider.chat.called


@pytest.mark.asyncio
async def test_quiz_task_does_not_call_llm(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    c1 = course_factory(id=10, name="Algoritmos")
    repo.upsert_courses(user.user_id, [c1])

    a = assignment_factory(
        id=30,
        course_id=10,
        name="ATIVIDADE OBJETIVA - M1",
        submission_types=["online_quiz"],
        due_at="2026-12-01T23:59:00Z",
        points_possible=5.0,
    )
    repo.replace_assignments(user.user_id, 10, [a])
    detail = repo.get_assignment_detail(user.user_id, 30)

    assert is_quiz_task(detail)

    fake_provider = AsyncMock()
    settings = Settings()
    res = await generate_task_briefing(repo, fake_provider, user, detail, tz, settings=settings)

    assert "Informações do Quiz" in res.text
    assert "Não geramos briefing de resolução nem respostas para questionários" in res.text
    assert "/quiz" in res.text
    assert not fake_provider.chat.called


def test_refusal_redirection() -> None:
    assert is_resolution_request("resolve pra mim essa tarefa")
    assert is_resolution_request("faça pra mim o trabalho")
    assert is_resolution_request("escreva a resposta da questão")
    assert not is_resolution_request("como posso planejar a entrega?")

    msg = format_refusal_message("Trabalho Final", "DAM")
    assert "/tarefa Trabalho Final" in msg
    assert "/quiz" in msg


@pytest.mark.asyncio
async def test_briefing_cache_avoids_second_llm_call(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    c1 = course_factory(id=10, name="Banco de Dados")
    repo.upsert_courses(user.user_id, [c1])

    a = assignment_factory(
        id=40,
        course_id=10,
        name="Modelagem ER",
        description_html="<p>Crie um modelo ER.</p>",
        due_at="2026-11-30T23:59:00Z",
    )
    repo.replace_assignments(user.user_id, 10, [a])
    detail = repo.get_assignment_detail(user.user_id, 40)

    fake_provider = AsyncMock()
    fake_provider.chat.return_value = LLMResponse(
        text="1. O que pedem:\nModelo ER.\n\n5. Plano:\nPasso 1.\n\n6. Perguntas:\nValidou?",
        input_tokens=100,
        output_tokens=50,
    )

    settings = Settings()
    res1 = await generate_task_briefing(repo, fake_provider, user, detail, tz, settings=settings)
    assert not res1.from_cache
    assert fake_provider.chat.call_count == 1

    res2 = await generate_task_briefing(repo, fake_provider, user, detail, tz, settings=settings)
    assert res2.from_cache
    assert fake_provider.chat.call_count == 1


def test_ambiguity_and_empty_description(
    repo, user, tz, course_factory, assignment_factory
) -> None:
    c1 = course_factory(id=10, name="Matéria Única")
    repo.upsert_courses(user.user_id, [c1])

    a1 = assignment_factory(id=51, course_id=10, name="Trabalho Prático - Fase 1")
    a2 = assignment_factory(id=52, course_id=10, name="Trabalho Prático - Fase 2")
    repo.replace_assignments(user.user_id, 10, [a1, a2])

    target, candidates, err = resolve_task_selection(repo, user.user_id, "Trabalho Prático", tz)
    assert target is None
    assert len(candidates) == 2
    assert not err

    target_num, candidates_num, _ = resolve_task_selection(repo, user.user_id, "1", tz)
    assert target_num is not None
    assert len(candidates_num) == 0


def test_days_remaining_label_formatting(tz) -> None:
    from datetime import datetime, timedelta

    base = datetime(2026, 11, 12, 10, 0, tzinfo=tz)

    # 1. Futuro distante
    future = base + timedelta(days=3, hours=5)
    label, is_late = _days_remaining_label(future, base)
    assert not is_late
    assert label.startswith("em 3 dias e 5 h")

    # 2. Hoje
    today = base + timedelta(hours=6)
    label_today, is_late_today = _days_remaining_label(today, base)
    assert not is_late_today
    assert label_today.startswith("HOJE, restam 6h")

    # 3. Atrasada
    past = base - timedelta(days=2)
    label_past, is_late_past = _days_remaining_label(past, base)
    assert is_late_past
    assert label_past.startswith("atrasada há 2d")


def test_submission_types_translation() -> None:
    translated = _translate_submission_types(["online_upload", "online_text_entry"])
    assert translated == "envio de arquivo, texto online"


def test_cut_at_word_boundary_and_strips_images() -> None:
    text = (
        "Trecho inicial com [Imagem: foto do diagrama] "
        "e muito mais texto para ser truncado de forma limpa."
    )
    cut = _cut_at_word_boundary(text, max_chars=40)
    assert "[Imagem:" not in cut
    assert cut.endswith("…")
    assert not cut.endswith(" …")
