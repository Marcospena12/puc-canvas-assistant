from __future__ import annotations

from typing import Any

from canvas_assistant.knowledge.canvas_html import clean_canvas_html
from canvas_assistant.knowledge.content_sync import ContentSyncService
from canvas_assistant.knowledge.search import search


def test_clean_canvas_html_comprehensive() -> None:
    raw_html = """
    <html>
        <head>
            <style>body { color: red; }</style>
            <script>alert("hack");</script>
        </head>
        <body>
            <h1>Unidade 1: Introdução</h1>
            <p>Parágrafo inicial com entidades &amp; s&iacute;mbolos &quot;aspas&quot;.</p>
            <h2>Objetivos</h2>
            <ul>
                <li>Primeiro objetivo</li>
                <li>Segundo objetivo</li>
            </ul>
            <table>
                <tr><th>Conceito</th><th>Descrição</th></tr>
                <tr><td>Fato</td><td>Métrica numérica</td></tr>
                <tr><td>Dimensão</td><td>Contexto descritivo</td></tr>
            </table>
            <img src="banner.png" alt="Diagrama de Estrela" />
            <img src="spacer.gif" />
            <a href="https://canvas.pucminas.br/files/54321/download">Arquivo da aula</a>
            <a href="https://youtube.com/watch?v=12345">Vídeo complementar</a>
        </body>
    </html>
    """
    res = clean_canvas_html(raw_html)

    # 1. Scripts e styles removidos
    assert "alert" not in res.text
    assert "color: red" not in res.text

    # 2. Cabeçalhos e entidades preservados
    assert "# Unidade 1: Introdução" in res.text
    assert "# Objetivos" in res.text
    assert '& s\xedmbolos "aspas"' in res.text or "& símbolos" in res.text or "símbolos" in res.text

    # 3. Listas e tabelas
    assert "• Primeiro objetivo" in res.text
    assert "Conceito | Descrição" in res.text
    assert "Fato | Métrica numérica" in res.text

    # 4. Imagem com e sem alt
    assert "[Imagem: Diagrama de Estrela]" in res.text
    assert res.images_without_alt == 1

    # 5. Detecção de arquivos do Canvas e links externos
    assert res.canvas_file_ids == {54321}
    assert any("youtube.com" in url for _, url in res.external_links)
    assert "Links:\n- Vídeo complementar: https://youtube.com/watch?v=12345" in res.text


def test_clean_canvas_html_empty_body() -> None:
    res = clean_canvas_html("   \n\t  ")
    assert res.text == ""
    assert res.canvas_file_ids == set()
    assert res.images_without_alt == 0


class FakeCanvasClientForContent:
    def __init__(
        self, course_id: int, pages: list[dict[str, Any]], assignments: list[dict[str, Any]]
    ) -> None:
        self.course_id = course_id
        self.pages = pages
        self.assignments = assignments

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == f"courses/{self.course_id}" or "include[]" in str(params):
            return {
                "syllabus_body": (
                    "<p>Ementa oficial e objetivos gerais da matéria de teste de sistemas web.</p>"
                )
            }
        for p in self.pages:
            if f"pages/{p['url']}" in path:
                return p
        return {}

    async def get_all(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        if "pages" in path:
            return [{"url": p["url"], "title": p["title"]} for p in self.pages]
        if "quizzes" in path:
            return []
        return []


class FakeCanvasServiceForContent:
    def __init__(self, client: FakeCanvasClientForContent) -> None:
        self.client = client

    async def get_modules(self, course_id: int) -> list[dict[str, Any]]:
        return []

    async def get_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return self.client.assignments

    async def get_announcements(
        self, course_id: int, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        return []


async def test_content_sync_incremental_and_removal(repo, user, tz, course_factory) -> None:
    c1 = course_factory(id=1, name="Sistemas Web")
    repo.upsert_courses(user.user_id, [c1])

    pages_initial = [
        {
            "url": "intro",
            "title": "Introdução Web",
            "body": "<h1>Bem-vindo</h1><p>Conceitos de arquitetura cliente-servidor.</p>",
        }
    ]
    assignments_initial = [
        {
            "id": 101,
            "name": "Trabalho 1",
            "due_at": "2026-11-20T23:59:00Z",
            "points_possible": 10.0,
            "submission_types": ["online_upload"],
            "description": "<p>Desenvolva uma página HTML com tabelas e formulários.</p>",
        }
    ]

    fake_client = FakeCanvasClientForContent(1, pages_initial, assignments_initial)
    fake_service = FakeCanvasServiceForContent(fake_client)
    sync_svc = ContentSyncService(repo, fake_service, user, tz)

    # 1ª Execução: Ingestão inicial
    res1 = await sync_svc.sync_all(notify_baseline=False)
    assert res1.syllabus_ingested == 1
    assert res1.pages_ingested == 1
    assert res1.assignments_ingested == 1
    assert res1.chunks_created >= 3

    # Consistência FTS
    consistency1 = repo.fts_consistency_check()
    assert consistency1["chunks_count"] == consistency1["fts_count"]
    assert consistency1["chunks_count"] >= 3

    # 2ª Execução idêntica: nada deve ser reingerido (hashes iguais)
    res2 = await sync_svc.sync_all(notify_baseline=True)
    assert res2.syllabus_ingested == 0
    assert res2.pages_ingested == 0
    assert res2.assignments_ingested == 0
    assert res2.chunks_created == 0

    # 3ª Execução: Atualização de enunciado na tarefa
    assignments_initial[0]["description"] = (
        "<p>Enunciado atualizado: desenvolva página HTML e adicione CSS responsivo.</p>"
    )
    res3 = await sync_svc.sync_all(notify_baseline=True)
    assert res3.assignments_ingested == 1
    assert any(ev.event_type == "updated_assignment" for ev in res3.events)

    # 4ª Execução: Página é despublicada / removida
    fake_client.pages = []
    await sync_svc.sync_all(notify_baseline=True)
    # A página despublicada deve ter chunks apagados
    page_state = repo.get_canvas_content_state(user.user_id, 1, "pagina", "intro")
    assert page_state is not None
    assert page_state["active"] == 0
    page_chunks = repo.get_chunks_by_content(user.user_id, 1, "pagina", "intro")
    assert len(page_chunks) == 0


def test_search_source_types_filter(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Engenharia de Software")
    repo.upsert_courses(user.user_id, [c1])

    # Cria chunks sintéticos de tipos diferentes
    repo.replace_content_chunks(
        user.user_id,
        1,
        "pagina",
        "p1",
        "Página Requisitos",
        [
            {
                "chunk_index": 1,
                "location_type": "pagina",
                "location_label": "Página: Requisitos",
                "text": "Requisitos funcionais de software.",
                "text_hash": "h1",
                "char_count": 30,
                "token_estimate": 8,
            }
        ],
    )
    repo.upsert_canvas_content_state(
        user.user_id,
        {
            "course_id": 1,
            "source_type": "pagina",
            "source_ref": "p1",
            "title": "Página Requisitos",
            "content_hash": "h1",
            "active": 1,
        },
    )

    repo.replace_content_chunks(
        user.user_id,
        1,
        "tarefa",
        "t1",
        "Tarefa Requisitos",
        [
            {
                "chunk_index": 1,
                "location_type": "tarefa",
                "location_label": "Tarefa: Requisitos",
                "text": "Entrega dos diagramas de requisitos funcionais.",
                "text_hash": "h2",
                "char_count": 40,
                "token_estimate": 10,
            }
        ],
        due_at="sex 20/11 23:59",
    )
    repo.upsert_canvas_content_state(
        user.user_id,
        {
            "course_id": 1,
            "source_type": "tarefa",
            "source_ref": "t1",
            "title": "Tarefa Requisitos",
            "content_hash": "h2",
            "active": 1,
        },
    )

    # 1. Filtro por tarefa
    res_tarefa = search(repo, user.user_id, "requisitos", source_types=["tarefa"])
    assert len(res_tarefa) == 1
    assert res_tarefa[0].source_type == "tarefa"
    assert res_tarefa[0].due_at == "sex 20/11 23:59"

    # 2. Filtro por página
    res_pagina = search(repo, user.user_id, "requisitos", source_types=["pagina"])
    assert len(res_pagina) == 1
    assert res_pagina[0].source_type == "pagina"

    # 3. Busca ampla sem filtro
    res_todos = search(repo, user.user_id, "requisitos")
    assert len(res_todos) == 2
    types_found = {it.source_type for it in res_todos}
    assert types_found == {"pagina", "tarefa"}
