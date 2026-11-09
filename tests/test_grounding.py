from __future__ import annotations

import json

from canvas_assistant.agent.grounding import (
    build_evidence,
    correction_message,
    deterministic_fallback,
    parse_tool_result,
    validate_answer,
)
from canvas_assistant.urls import normalize_url

ITEM_URL = "https://canvas.test/courses/292828/assignments/1404750"
FAKE_URL = "https://canvas.test/courses/292828/assignments/1404751"


def upcoming_observation():
    payload = {
        "ok": True,
        "period": "next_7_days",
        "items": [
            {
                "id": 1404750,
                "course": (
                    "Trabalho Interdisciplinar: Pesquisa em Informática - "
                    "Sistemas de Informação - Campus Coração Eucarístico"
                ),
                "title": "Elaboração de projeto (passo 2)",
                "due_at": "seg 09/11 23:59",
                "status": "pendente",
                "points": 15,
                "url": ITEM_URL,
            }
        ],
        "count": 1,
        "note": None,
    }
    return parse_tool_result("get_upcoming_items", {"period": "next_7_days"}, json.dumps(payload))


def test_validate_answer_accepts_grounded_url_and_id() -> None:
    observation = upcoming_observation()
    answer = f"Você tem uma tarefa: [abrir]({ITEM_URL})."

    assert validate_answer(answer, observation.urls) == set()


def test_validate_answer_rejects_invented_url_and_id() -> None:
    observation = upcoming_observation()
    answer = f"Você tem duas tarefas: [abrir]({FAKE_URL}) e /assignments/999999."

    unknown = validate_answer(answer, observation.urls)

    assert normalize_url(FAKE_URL) in unknown
    assert "id 999999" in unknown


def test_deterministic_fallback_uses_tool_data_and_short_name() -> None:
    observation = upcoming_observation()

    answer = deterministic_fallback([observation])

    assert answer is not None
    assert "Elaboração de projeto (passo 2)" in answer
    assert "Trabalho Interdisciplinar: Pesquisa em Informática" in answer
    assert "Campus Coração Eucarístico" not in answer
    assert ITEM_URL in answer
    assert FAKE_URL not in answer


def test_build_evidence_collects_urls_and_truncates() -> None:
    observation = upcoming_observation()

    evidence = build_evidence([observation], limit=80)

    assert ITEM_URL in evidence.urls
    assert len(evidence.text) <= 81


def test_correction_message_lists_unknown_data() -> None:
    message = correction_message({FAKE_URL})

    assert "não confirmados" in message
    assert FAKE_URL in message
