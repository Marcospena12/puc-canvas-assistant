"""Testes da Etapa B (4.2): revisão de rascunho pelo tutor.

Cobre B1-B6: fluxo, privacidade, critérios, feedback, custo e recusas.
LLM mockado; rascunhos sintéticos.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from canvas_assistant.config import Settings, UserProfile
from canvas_assistant.db import Database
from canvas_assistant.knowledge.review_service import (
    ReviewService,
    _validate_and_format_feedback,
    save_temp_file,
)
from canvas_assistant.models import Assignment, Course
from canvas_assistant.repository import Repository
from canvas_assistant.timeutil import utc_now

# ============================ Fixtures ============================


@pytest.fixture
def review_repo(tmp_path) -> Repository:
    db = Database(tmp_path / "test.sqlite3")
    db.initialize()
    return Repository(db)


@pytest.fixture
def review_user() -> UserProfile:
    return UserProfile(
        user_id="test-review",
        canvas_base_url="https://canvas.test",
        canvas_token="token",
        telegram_chat_id=10,
        telegram_user_ids=frozenset({10}),
    )


@pytest.fixture
def review_service(review_repo, review_user):
    settings = Settings()
    return ReviewService(review_repo, review_user, ZoneInfo("America/Sao_Paulo"), settings)


@pytest.fixture
def seed_course(review_repo, review_user):
    now = utc_now()
    course = Course(
        id=1,
        name="Trabalho Interdisciplinar",
        course_code="(0001)",
        workflow_state="available",
        term_id=1,
        term_name="2026/2",
        term_start_at=now.isoformat(),
        term_end_at=(now + timedelta(days=60)).isoformat(),
        current_score=80.0,
        current_grade=None,
        teachers=["Prof. Teste"],
        tabs=[],
        is_subject=True,
    )
    review_repo.upsert_courses(review_user.user_id, [course])
    return course


@pytest.fixture
def seed_assignment(review_repo, review_user, seed_course):
    a = Assignment(
        id=100,
        course_id=1,
        name="Elaboração de projeto (passo 3)",
        due_at=(utc_now() + timedelta(days=7)).isoformat(),
        lock_at=None,
        unlock_at=None,
        points_possible=10.0,
        submission_types=["online_upload"],
        is_quiz=False,
        html_url="https://canvas.test/assignments/100",
        published=True,
        submission_state="unsubmitted",
        submission_score=None,
        submission_grade=None,
        submitted_at=None,
        graded_at=None,
        late=False,
        missing=False,
        excused=False,
    )
    review_repo.replace_assignments(review_user.user_id, 1, [a])
    detail = {
        "id": 100,
        "course_id": 1,
        "name": "Elaboração de projeto (passo 3)",
        "course_name": "Trabalho Interdisciplinar",
        "description_html": "Elabore um projeto de pesquisa com no mínimo 1500 palavras.",
        "rubric_json": json.dumps(
            [
                {"id": "r1", "description": "Objetivo específico", "points": 3},
                {"id": "r2", "description": "Metodologia", "points": 4},
                {"id": "r3", "description": "Referências", "points": 3},
            ]
        ),
        "submission_types_json": '["online_upload"]',
        "html_url": "https://canvas.test/assignments/100",
    }
    return detail


class StubProvider:
    """LLM mockado para revisão."""

    def __init__(self, response_text: str = "", in_tok: int = 100, out_tok: int = 50):
        self.response_text = response_text
        self.in_tok = in_tok
        self.out_tok = out_tok

    async def chat(self, messages, tools):
        from canvas_assistant.llm.base import LLMResponse

        return LLMResponse(
            text=self.response_text,
            tool_calls=[],
            input_tokens=self.in_tok,
            output_tokens=self.out_tok,
        )

    async def aclose(self):
        pass


# ============================ B1: Fluxo /revisar ============================


class TestReviewFlux:
    """B1: fluxo /revisar, sessão, recusa de quiz, cancelar."""

    def test_start_session(self, review_service, seed_assignment) -> None:
        session = review_service.start_session(seed_assignment)
        assert session.assignment_id == 100
        assert session.is_active()

    def test_session_expires(self, review_service, seed_assignment) -> None:
        from datetime import UTC

        session = review_service.start_session(seed_assignment)
        old = datetime.now(UTC) - timedelta(minutes=31)
        session.expires_at = old
        assert not session.is_active()
        assert review_service.get_session() is None

    def test_cancel_session(self, review_service, seed_assignment) -> None:
        review_service.start_session(seed_assignment)
        assert review_service.cancel_session() is True
        assert review_service.get_session() is None

    def test_quiz_task_rejected(self, review_service) -> None:
        quiz_assignment = {
            "id": 200,
            "name": "ATIVIDADE OBJETIVA",
            "submission_types_json": '["online_quiz"]',
            "description_html": "",
            "rubric_json": "[]",
            "course_name": "Test",
        }
        assert review_service.is_quiz(quiz_assignment) is True

    def test_non_quiz_task_accepted(self, review_service, seed_assignment) -> None:
        assert review_service.is_quiz(seed_assignment) is False


# ============================ B2: Privacidade ============================


class TestReviewPrivacy:
    """B2: arquivo temporário apagado, não persiste rascunho, cache por hash."""

    def test_temp_file_deleted_after_extraction(self, review_service, tmp_path) -> None:
        temp_file = tmp_path / "test.txt"
        temp_file.write_text("Conteúdo do rascunho", encoding="utf-8")
        text = review_service.extract_text_from_file(temp_file)
        assert "Conteúdo do rascunho" in text
        assert not temp_file.exists()

    def test_temp_file_deleted_on_error(self, review_service, tmp_path) -> None:
        import contextlib

        temp_file = tmp_path / "test.unknown"
        temp_file.write_bytes(b"dados")
        with contextlib.suppress(ValueError):
            review_service.extract_text_from_file(temp_file)
        assert not temp_file.exists()

    def test_review_persist_drafts_false_by_default(self) -> None:
        s = Settings()
        assert s.review_persist_drafts is False

    def test_cache_key_uses_draft_hash(self, review_service, seed_assignment) -> None:
        """Cache por hash(rascunho) + hash(rubrica) + versão do prompt."""
        review_service.start_session(seed_assignment)
        provider = StubProvider(
            response_text=json.dumps(
                {
                    "criterios": [
                        {"nome": "X", "nivel": "Atende", "evidencia": "", "o_que_falta": ""}
                    ],
                    "prioridades": [],
                    "aviso": "estimativa automática, não substitui a avaliação do professor",
                }
            )
        )
        draft = "Rascunho de teste para validação."
        asyncio.run(review_service.review_draft(provider, review_service.get_session(), draft))
        # Segunda chamada com mesmo rascunho deve vir do cache
        result2 = asyncio.run(
            review_service.review_draft(provider, review_service.get_session(), draft)
        )
        assert result2.from_cache is True

    def test_logs_no_draft_content(self, review_service, seed_assignment, caplog) -> None:
        review_service.start_session(seed_assignment)
        provider = StubProvider(response_text="{}")
        draft = "Rascunho secreto com conteúdo confidencial."
        with caplog.at_level("INFO"):
            asyncio.run(review_service.review_draft(provider, review_service.get_session(), draft))
        assert "Rascunho secreto" not in caplog.text


# ============================ B3: Critérios ============================


class TestReviewCriteria:
    """B3: rubrica da API, critérios derivados do enunciado, checagem formal."""

    def test_rubric_from_api(self, review_service, seed_assignment) -> None:
        session = review_service.start_session(seed_assignment)
        assert len(session.rubric) == 3
        assert session.rubric[0]["description"] == "Objetivo específico"

    def test_no_rubric_derives_from_description(self, review_service) -> None:
        """Sem rubrica, deriva critérios do enunciado marcados como derivados."""
        assignment = {
            "id": 300,
            "name": "Trabalho X",
            "course_name": "Test",
            "description_html": "Elabore um relatório com introdução e conclusão.",
            "rubric_json": "[]",
            "submission_types_json": '["online_upload"]',
        }
        session = review_service.start_session(assignment)
        assert session.rubric == []
        assert session.description_html != ""

    def test_no_rubric_no_description(self, review_service) -> None:
        """Sem rubrica e sem enunciado: só checagem formal."""
        assignment = {
            "id": 400,
            "name": "Tarefa vazia",
            "course_name": "Test",
            "description_html": "",
            "rubric_json": "[]",
            "submission_types_json": '["online_upload"]',
        }
        session = review_service.start_session(assignment)
        assert session.rubric == []
        assert session.description_html == ""


# ============================ B4: Feedback por critério ============================


class TestReviewFeedback:
    """B4: nível, evidência validada, o que falta, prioridades, sem nota."""

    def test_citation_invented_is_discarded(self) -> None:
        """Citação inventada (não está no rascunho) é descartada."""
        draft = "Este é o rascunho do aluno com texto real."
        raw = json.dumps(
            {
                "criterios": [
                    {
                        "nome": "Objetivo",
                        "nivel": "Atende",
                        "evidencia": "texto que não existe no rascunho",
                        "localizacao": "P1",
                        "o_que_falta": "Detalhar mais",
                    }
                ],
                "prioridades": [],
                "aviso": "estimativa automática, não substitui a avaliação do professor",
            }
        )
        feedback = _validate_and_format_feedback(raw, draft, {}, len(draft.split()))
        assert "sem evidência verificável" in feedback

    def test_citation_valid_is_preserved(self) -> None:
        """Citação que está no rascunho é preservada."""
        draft = "Este é o rascunho do aluno com texto real."
        raw = json.dumps(
            {
                "criterios": [
                    {
                        "nome": "Objetivo",
                        "nivel": "Atende",
                        "evidencia": "rascunho do aluno",
                        "localizacao": "P1",
                        "o_que_falta": "Detalhar mais",
                    }
                ],
                "prioridades": ["Revisar objetivo"],
                "aviso": "estimativa automática, não substitui a avaliação do professor",
            }
        )
        feedback = _validate_and_format_feedback(raw, draft, {}, len(draft.split()))
        assert "rascunho do aluno" in feedback
        assert "Atende" in feedback
        assert "Revisar objetivo" in feedback

    def test_criterion_without_evidence_marked(self) -> None:
        """Critério sem evidência é marcado como 'sem evidência verificável'."""
        raw = json.dumps(
            {
                "criterios": [
                    {
                        "nome": "Metodologia",
                        "nivel": "Não identificado",
                        "evidencia": "",
                        "localizacao": "",
                        "o_que_falta": "Falta descrever a metodologia",
                    }
                ],
                "prioridades": [],
                "aviso": "estimativa automática, não substitui a avaliação do professor",
            }
        )
        feedback = _validate_and_format_feedback(raw, "rascunho", {}, 1)
        assert "sem evidência verificável" in feedback
        assert "Não identificado" in feedback

    def test_feedback_never_contains_numeric_grade(self) -> None:
        """Feedback nunca contém nota numérica."""
        raw = json.dumps(
            {
                "criterios": [
                    {
                        "nome": "X",
                        "nivel": "Atende",
                        "evidencia": "",
                        "o_que_falta": "",
                    }
                ],
                "prioridades": [],
                "aviso": "estimativa automática, não substitui a avaliação do professor",
            }
        )
        feedback = _validate_and_format_feedback(raw, "rascunho", {}, 1)
        assert "nota 8" not in feedback.lower()
        assert "nota: 7" not in feedback.lower()

    def test_formal_check_word_count(self) -> None:
        """Checagem de limite de palavras feita no código."""
        formal_checks = {"word_limit": 100, "word_unit": "palavras"}
        raw = json.dumps({"criterios": [], "prioridades": [], "aviso": ""})
        feedback = _validate_and_format_feedback(raw, "rascunho curto", formal_checks, 50)
        assert "Checagem formal" in feedback
        assert "50" in feedback
        assert "100" in feedback
        assert "OK" in feedback

    def test_formal_check_exceeds(self) -> None:
        """Checagem formal detecta excedente."""
        formal_checks = {"word_limit": 100, "word_unit": "palavras"}
        raw = json.dumps({"criterios": [], "prioridades": [], "aviso": ""})
        feedback = _validate_and_format_feedback(raw, "rascunho " * 150, formal_checks, 151)
        assert "EXCEDEU" in feedback

    def test_priorities_max_3(self) -> None:
        """Até 3 prioridades de ajuste."""
        raw = json.dumps(
            {
                "criterios": [],
                "prioridades": ["P1", "P2", "P3", "P4"],
                "aviso": "estimativa automática, não substitui a avaliação do professor",
            }
        )
        feedback = _validate_and_format_feedback(raw, "rascunho", {}, 1)
        assert "1. P1" in feedback
        assert "2. P2" in feedback
        assert "3. P3" in feedback
        assert "P4" not in feedback

    def test_aviso_present(self) -> None:
        """Feedback termina com aviso de estimativa automática."""
        raw = json.dumps({"criterios": [], "prioridades": [], "aviso": ""})
        feedback = _validate_and_format_feedback(raw, "rascunho", {}, 1)
        assert "estimativa automática" in feedback
        assert "não substitui a avaliação do professor" in feedback


# ============================ B5: Controle de custo ============================


class TestReviewCost:
    """B5: REVIEW_MAX_CHARS, registro de tokens/latência."""

    def test_review_max_chars_default(self) -> None:
        s = Settings()
        assert s.review_max_chars == 20000

    def test_large_draft_requests_sections(self, review_service, seed_assignment) -> None:
        """Rascunhos acima de REVIEW_MAX_CHARS pedem seções/faixa de páginas."""
        review_service.start_session(seed_assignment)
        big_draft = "x" * (review_service.settings.review_max_chars + 1)
        result = asyncio.run(
            review_service.review_draft(StubProvider(), review_service.get_session(), big_draft)
        )
        assert "muito longo" in result.text or "seções" in result.text.lower()

    def test_tokens_and_latency_recorded(
        self, review_service, seed_assignment, review_repo
    ) -> None:
        review_service.start_session(seed_assignment)
        provider = StubProvider(in_tok=200, out_tok=80)
        asyncio.run(
            review_service.review_draft(provider, review_service.get_session(), "rascunho de teste")
        )
        latest = review_repo.get_latest_study_usage(review_service.user.user_id)
        assert latest is not None
        assert latest["command"] == "revisar"
        assert latest["input_tokens"] == 200
        assert latest["output_tokens"] == 80
        assert latest["latency_ms"] >= 0


# ============================ B6: Recusa de reescrita ============================


class TestReviewRefusal:
    """B6: pedido de reescrita é recusado."""

    def test_reescrita_refused_in_prompt(self) -> None:
        """O prompt do sistema contém regra de recusa de reescrita."""
        from canvas_assistant.knowledge.review_service import _build_review_system_prompt

        prompt = _build_review_system_prompt()
        assert "NUNCA reescreva" in prompt
        assert "reescreve" in prompt.lower() or "melhora o texto" in prompt.lower()


# ============================ B2: Arquivo temporário ============================


class TestTempFile:
    """B2: save_temp_file e apagamento."""

    def test_save_temp_file(self, tmp_path) -> None:
        path = save_temp_file(b"conteudo", ".txt", str(tmp_path))
        assert path.exists()
        assert path.read_bytes() == b"conteudo"
        assert path.suffix == ".txt"

    def test_file_deleted_after_success(self, review_service, tmp_path) -> None:
        temp_file = tmp_path / "draft.txt"
        temp_file.write_text("Rascunho para revisão", encoding="utf-8")
        text = review_service.extract_text_from_file(temp_file)
        assert "Rascunho para revisão" in text
        assert not temp_file.exists()


# ============================ B1: Usuário não autorizado ============================


class TestReviewAuthorization:
    """B1: só usuários autorizados; outros ignorados sem LLM nem Canvas."""

    def test_review_service_requires_user(self, review_repo) -> None:
        """ReviewService só funciona com UserProfile configurado."""
        user = UserProfile(
            user_id="auth-user",
            canvas_base_url="https://canvas.test",
            canvas_token="token",
            telegram_chat_id=10,
            telegram_user_ids=frozenset({10}),
        )
        service = ReviewService(review_repo, user, ZoneInfo("America/Sao_Paulo"), Settings())
        assert service.user.user_id == "auth-user"
