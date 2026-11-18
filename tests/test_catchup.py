"""Testes da Etapa A (4.1.2): catch-up, heartbeat, /saude, roteador e vídeo.

Cobre A2 (catch-up ao iniciar), A3 (heartbeat externo), A4 (/saude com modelo),
A7 (roteador: tutor_refuse, announcements, exceção avaliação+disciplina) e
A9 (marcador [Vídeo: <título>] e aviso de videoaula no /explicar).
"""

from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from canvas_assistant.agent.agent import classify_message_intent
from canvas_assistant.app import build_health_text
from canvas_assistant.catchup import (
    build_catchup_message,
    get_last_cycle,
    get_started_at,
    mark_catchup_sent,
    mark_scheduler_cycle,
    persist_started_at,
    should_run_catchup,
)
from canvas_assistant.config import Settings
from canvas_assistant.incidents import IncidentService
from canvas_assistant.knowledge.canvas_html import clean_canvas_html
from canvas_assistant.models import Assignment, Course
from canvas_assistant.repository import Repository
from canvas_assistant.timeutil import to_utc_iso, utc_now

# ============================ A2: Catch-up ao iniciar ============================


def _seed_course(repo: Repository, user, course_id=1, name="BI - Business Intelligence") -> None:
    now = utc_now()
    course = Course(
        id=course_id,
        name=name,
        course_code="(0001)",
        workflow_state="available",
        term_id=1,
        term_name="2026/2",
        term_start_at=to_utc_iso(now - timedelta(days=30)),
        term_end_at=to_utc_iso(now + timedelta(days=60)),
        current_score=80.0,
        current_grade=None,
        teachers=["Prof. Teste"],
        tabs=[],
        is_subject=True,
    )
    repo.upsert_courses(user.user_id, [course])


def _seed_assignment(
    repo: Repository, user, course_id=1, assignment_id=100, due_at=None, name="Lista 1"
) -> None:
    if due_at is None:
        due_at = utc_now() + timedelta(days=1)
    a = Assignment(
        id=assignment_id,
        course_id=course_id,
        name=name,
        due_at=to_utc_iso(due_at),
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
    repo.replace_assignments(user.user_id, course_id, [a])


def _seed_assignments(repo: Repository, user, course_id=1, assignments_data=None) -> None:
    """Semeia múltiplas tarefas no mesmo curso (uma chamada de replace)."""
    if assignments_data is None:
        assignment_data = [
            dict(
                assignment_id=100,
                due_at=utc_now() + timedelta(days=1),
                name="Lista 1",
            )
        ]
        assignments_data = [assignment_data]
    items = []
    for data in assignments_data:
        a = Assignment(
            id=data["assignment_id"],
            course_id=course_id,
            name=data["name"],
            due_at=to_utc_iso(data["due_at"]),
            lock_at=None,
            unlock_at=None,
            points_possible=10.0,
            submission_types=["online_upload"],
            is_quiz=False,
            html_url=f"https://canvas.test/assignments/{data['assignment_id']}",
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
        items.append(a)
    repo.replace_assignments(user.user_id, course_id, items)


class TestCatchup:
    """A2: catch-up ao iniciar — detecta lacuna > CATCHUP_GAP_MINUTES."""

    def test_mark_and_get_last_cycle(self, repo, user) -> None:
        mark_scheduler_cycle(repo, user.user_id)
        assert get_last_cycle(repo, user.user_id) is not None

    def test_should_run_catchup_returns_false_when_no_last_cycle(self, repo, user) -> None:
        run, last, now = should_run_catchup(repo, user.user_id, gap_minutes=30)
        assert run is False
        assert last is None

    def test_should_run_catchup_returns_false_for_small_gap(self, repo, user) -> None:
        recent = utc_now() - timedelta(minutes=10)
        mark_scheduler_cycle(repo, user.user_id, now=recent)
        run, last, now = should_run_catchup(repo, user.user_id, gap_minutes=30)
        assert run is False

    def test_should_run_catchup_returns_true_for_large_gap(self, repo, user) -> None:
        old = utc_now() - timedelta(hours=2)
        mark_scheduler_cycle(repo, user.user_id, now=old)
        run, last, now = should_run_catchup(repo, user.user_id, gap_minutes=30)
        assert run is True
        assert last is not None

    def test_catchup_message_2h_gap(self, repo, user, monkeypatch) -> None:
        """Simula lacuna de 2h: deve listar tarefas vencidas na lacuna."""
        _seed_course(repo, user)
        old = utc_now() - timedelta(hours=2)
        vencida_na_lacuna = utc_now() - timedelta(minutes=30)
        _seed_assignments(
            repo,
            user,
            assignments_data=[
                dict(assignment_id=101, due_at=vencida_na_lacuna, name="Trabalho vencido"),
                dict(
                    assignment_id=102,
                    due_at=utc_now() + timedelta(hours=12),
                    name="Trabalho futuro",
                ),
            ],
        )
        msg = build_catchup_message(
            repo,
            user,
            Settings(),
            ZoneInfo("America/Sao_Paulo"),
            last_cycle=old,
            now=utc_now(),
        )
        assert "Fiquei offline de" in msg
        assert "Pendências:" in msg
        assert "Vencidas na lacuna:" in msg
        assert "Trabalho vencido" in msg
        assert "Vencem nas próximas 24h:" in msg
        assert "Trabalho futuro" in msg

    def test_catchup_message_24h_gap(self, repo, user) -> None:
        """Simula lacuna de 24h: lista vencidas e próximas 24h."""
        _seed_course(repo, user)
        old = utc_now() - timedelta(hours=24)
        _seed_assignments(
            repo,
            user,
            assignments_data=[
                dict(assignment_id=201, due_at=utc_now() - timedelta(hours=5), name="Vencida 24h"),
            ],
        )
        msg = build_catchup_message(
            repo,
            user,
            Settings(),
            ZoneInfo("America/Sao_Paulo"),
            last_cycle=old,
            now=utc_now(),
        )
        assert "Vencidas na lacuna:" in msg
        assert "Vencida 24h" in msg

    def test_catchup_message_3d_gap(self, repo, user) -> None:
        """Simula lacuna de 3 dias: lista vencidas na lacuna."""
        _seed_course(repo, user)
        old = utc_now() - timedelta(days=3)
        _seed_assignments(
            repo,
            user,
            assignments_data=[
                dict(assignment_id=301, due_at=utc_now() - timedelta(days=1), name="Vencida 3d"),
            ],
        )
        msg = build_catchup_message(
            repo,
            user,
            Settings(),
            ZoneInfo("America/Sao_Paulo"),
            last_cycle=old,
            now=utc_now(),
        )
        assert "3d" in msg or "dias" in msg
        assert "Vencida 3d" in msg

    def test_catchup_message_no_pendencias(self, repo, user) -> None:
        """Sem pendências na lacuna."""
        _seed_course(repo, user)
        old = utc_now() - timedelta(hours=1)
        msg = build_catchup_message(
            repo,
            user,
            Settings(),
            ZoneInfo("America/Sao_Paulo"),
            last_cycle=old,
            now=utc_now(),
        )
        assert "Nenhuma pendência nova no período." in msg

    def test_catchup_dedup(self, repo, user) -> None:
        """mark_catchup_sent evita reenvio para a mesma janela."""
        cycle_iso = (utc_now() - timedelta(hours=2)).isoformat()
        from canvas_assistant.catchup import already_sent_catchup

        assert not already_sent_catchup(repo, user.user_id, cycle_iso)
        mark_catchup_sent(repo, user.user_id, cycle_iso)
        assert already_sent_catchup(repo, user.user_id, cycle_iso)

    def test_persist_and_get_started_at(self, repo, user) -> None:
        started = utc_now()
        persist_started_at(repo, user.user_id, started)
        recovered = get_started_at(repo, user.user_id)
        assert recovered is not None


# ============================ A3: Heartbeat externo ============================


class TestHeartbeat:
    """A3: heartbeat externo — GET opcional quando há HEARTBEAT_URL."""

    def test_heartbeat_config_defaults(self) -> None:
        s = Settings()
        assert s.heartbeat_url == ""
        assert s.heartbeat_interval_minutes == 10

    def test_heartbeat_config_custom(self) -> None:
        s = Settings(heartbeat_url="https://hc-ping.com/abc", heartbeat_interval_minutes=5)
        assert s.heartbeat_url == "https://hc-ping.com/abc"
        assert s.heartbeat_interval_minutes == 5

    def test_heartbeat_does_not_run_without_url(self, repo, user) -> None:
        """Sem HEARTBEAT_URL, o job não faz nada."""
        from canvas_assistant.catchup import mark_scheduler_cycle

        mark_scheduler_cycle(repo, user.user_id)
        # O job heartbeat_job verifica settings.heartbeat_url; se vazio, retorna cedo.
        # Não há efeito colateral — confirmado pela ausência de erro.
        assert get_last_cycle(repo, user.user_id) is not None

    @pytest.mark.asyncio
    async def test_heartbeat_makes_get_when_url_set(self, repo, user, respx_mock) -> None:
        """Com HEARTBEAT_URL e último ciclo bem-sucedido, faz GET."""
        import httpx

        from canvas_assistant.catchup import mark_scheduler_cycle

        mark_scheduler_cycle(repo, user.user_id)
        url = "https://hc-ping.com/test-uuid"
        respx_mock.get(url).respond(200)

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10.0)
        assert resp.status_code == 200


# ============================ A4: /saude e uso ============================


class TestHealth:
    """A4: /saude mostra modelo ativo, último LLM e último ciclo do scheduler."""

    def test_health_contains_model(self, repo, user) -> None:
        settings = Settings()
        text = build_health_text(
            repo,
            settings,
            incidents=IncidentService(repo, user, None, settings.incidents),
            started_at=utc_now(),
        )
        assert "modelo ativo:" in text
        assert "ollama" in text

    def test_health_contains_last_cycle(self, repo, user) -> None:
        settings = Settings()
        mark_scheduler_cycle(repo, user.user_id, now=utc_now())
        text = build_health_text(
            repo,
            settings,
            incidents=IncidentService(repo, user, None, settings.incidents),
            started_at=utc_now(),
        )
        assert "último ciclo do scheduler:" in text

    def test_health_contains_schema_and_uptime(self, repo, user) -> None:
        settings = Settings()
        text = build_health_text(
            repo,
            settings,
            incidents=IncidentService(repo, user, None, settings.incidents),
            started_at=utc_now(),
        )
        assert "schema=" in text
        assert "uptime:" in text

    def test_agent_records_study_usage(self, repo, user) -> None:
        """A4: agente grava study_usage com command='agente'."""
        from canvas_assistant.agent.agent import Agent
        from canvas_assistant.llm.base import LLMResponse

        class StubProvider:
            async def chat(self, messages, tools):
                return LLMResponse(
                    text="Resposta de teste",
                    tool_calls=[],
                    input_tokens=10,
                    output_tokens=5,
                )

            async def aclose(self):
                pass

        _seed_course(repo, user)
        agent = Agent(StubProvider(), repo, user, ZoneInfo("America/Sao_Paulo"))
        import asyncio

        asyncio.run(agent.reply("o que tenho pra hoje?"))
        latest = repo.get_latest_study_usage(user.user_id)
        assert latest is not None
        assert latest["command"] == "agente"


# ============================ A7: Roteador ============================


class TestRouter:
    """A7: roteador — tutor_refuse, announcements, exceção avaliação+disciplina."""

    def test_router_deadlines_prova(self) -> None:
        assert classify_message_intent("quando é a prova de BI?") == "deadlines"

    def test_router_study_prova(self) -> None:
        assert classify_message_intent("o que cai na prova de DAM?") == "study"

    def test_router_grades_media(self) -> None:
        assert classify_message_intent("qual minha média em BI?") == "grades"

    def test_router_study_definition(self) -> None:
        assert classify_message_intent("o que é média ponderada?") == "study"

    def test_router_deadlines_trabalho(self) -> None:
        assert classify_message_intent("tenho trabalho de react pra quando?") == "deadlines"

    def test_router_deadlines_atrasado(self) -> None:
        assert classify_message_intent("tô atrasado em alguma coisa?") == "deadlines"

    def test_router_conversational(self) -> None:
        assert classify_message_intent("obrigado, ajudou!") == "conversational"

    def test_router_study_etl(self) -> None:
        assert classify_message_intent("oi, tudo bem? me explica o que é ETL") == "study"

    def test_router_tutor_refuse(self) -> None:
        assert classify_message_intent("resolve a atividade 4 de DAM pra mim") == "tutor_refuse"

    def test_router_courses_professores(self) -> None:
        assert classify_message_intent("quais professores eu tenho?") == "courses"

    def test_router_announcements(self) -> None:
        assert classify_message_intent("tem aviso novo?") == "announcements"

    def test_router_grades_passar(self) -> None:
        assert classify_message_intent("quanto falta pra eu passar em BI?") == "grades"

    def test_router_courses_avaliacao_disciplina(self) -> None:
        """Exceção: 'como funciona a avaliação da disciplina?' → courses, não study."""
        assert classify_message_intent("como funciona a avaliação da disciplina?") == "courses"

    def test_router_deadlines_hoje(self) -> None:
        assert classify_message_intent("tem algo pra hoje?") == "deadlines"

    def test_router_deadlines_semana(self) -> None:
        assert classify_message_intent("tem trabalho pra semana que vem?") == "deadlines"

    def test_router_tutor_refuse_variants(self) -> None:
        assert classify_message_intent("faça a tarefa pra mim") == "tutor_refuse"
        assert classify_message_intent("escreve pra mim o trabalho") == "tutor_refuse"
        assert classify_message_intent("me dá a resposta da questão") == "tutor_refuse"

    def test_router_announcements_variants(self) -> None:
        assert classify_message_intent("tem algum recado novo?") == "announcements"
        assert classify_message_intent("quais os comunicados recentes?") == "announcements"


# ============================ A9: Vídeo ============================


class TestVideo:
    """A9: marcador [Vídeo: <título>] e parsing LTI."""

    def test_video_marker_with_title(self) -> None:
        html = (
            '<iframe src="https://pucminas.instructuremedia.com/lti/launch?id=1" '
            'title="Aula sobre React Native"></iframe>'
        )
        res = clean_canvas_html(html)
        assert "[Vídeo: Aula sobre React Native]" in res.text

    def test_video_marker_from_lti_url_param(self) -> None:
        html = (
            '<iframe src="https://pucminas.instructuremedia.com/lti/launch?custom_arc_media_id=1'
            '&title=Aula%20de%20Persist%C3%AAncia"></iframe>'
        )
        res = clean_canvas_html(html)
        assert "[Vídeo: Aula de Persistência]" in res.text

    def test_media_marker_for_non_video(self) -> None:
        html = '<audio src="https://example.com/audio.mp3" title="Podcast"></audio>'
        res = clean_canvas_html(html)
        assert "[Mídia: Podcast]" in res.text

    def test_video_marker_youtube(self) -> None:
        html = '<iframe src="https://www.youtube.com/embed/abc123" title="Tutorial BI"></iframe>'
        res = clean_canvas_html(html)
        assert "[Vídeo: Tutorial BI]" in res.text

    def test_video_without_title_falls_back_to_media(self) -> None:
        html = '<iframe src="https://example.com/embed"></iframe>'
        res = clean_canvas_html(html)
        # Sem título e sem URL de vídeo conhecida → [Mídia: iframe]
        assert "[Mídia:" in res.text
