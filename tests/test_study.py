from __future__ import annotations

import hashlib
from typing import Any

from canvas_assistant.channels.telegram import split_text
from canvas_assistant.knowledge.study_context import (
    _task_has_body,
    build_context,
    build_study_system_prompt,
    calculate_grounding_overlap,
    extract_content_stems,
    post_process_study_answer,
    sanitize_answer_urls,
)
from canvas_assistant.knowledge.study_service import (
    _jaccard_similarity,
    explain_topic,
    generate_quiz,
    get_quiz_answer_key,
    summarize_file,
)
from canvas_assistant.llm.base import LLMResponse, Message, ToolSpec


class MockLLMProvider:
    def __init__(self, responses: list[LLMResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[list[Message], list[ToolSpec]]] = []
        self.closed = False

    async def chat(self, messages: list[Message], tools: list[ToolSpec]) -> LLMResponse:
        self.calls.append((messages, tools))
        if not self.responses:
            raise RuntimeError("Nenhuma resposta mock preparada")
        resp = self.responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def aclose(self) -> None:
        self.closed = True


def seed_file_and_chunks(
    repo,
    user,
    course_id: int,
    file_id: int,
    filename: str,
    chunks_text: list[str],
    sha256: str = "sha_study_test",
) -> None:
    repo.upsert_file_sync_states(
        user.user_id,
        [
            {
                "file_id": file_id,
                "course_id": course_id,
                "display_name": filename,
                "filename": filename,
                "content_type": "application/pdf",
                "size": 2048,
                "modified_at": "2026-11-01T12:00:00+00:00",
                "local_path": f"{course_id}/{file_id}_{filename}",
                "sha256": sha256,
                "status": "downloaded",
                "first_seen_at": "2026-11-01T12:00:00+00:00",
                "updated_at": "2026-11-01T12:00:00+00:00",
                "active": 1,
            }
        ],
    )
    repo.save_extracted_content(
        sha256,
        status="extraido",
        total_chunks=len(chunks_text),
        total_characters=sum(len(t) for t in chunks_text),
    )
    chunks_data: list[dict[str, Any]] = []
    for idx, t in enumerate(chunks_text, start=1):
        thash = hashlib.sha256(t.encode("utf-8")).hexdigest()
        chunks_data.append(
            {
                "chunk_index": idx,
                "location_type": "slide",
                "location_label": f"Slide {idx}",
                "location_start": idx,
                "location_end": idx,
                "text": t,
                "text_hash": thash,
                "char_count": len(t),
                "token_estimate": len(t) // 4,
            }
        )
    repo.replace_file_chunks(user.user_id, course_id, file_id, sha256, chunks_data)


async def test_prompt_contains_blocks_and_grounding_rules(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Business Intelligence")
    repo.upsert_courses(user.user_id, [c1])
    seed_file_and_chunks(
        repo,
        user,
        course_id=1,
        file_id=10,
        filename="Aula_BI.pdf",
        chunks_text=["Modelagem dimensional organiza dados em tabelas fato e tabelas dimensão."],
    )

    provider = MockLLMProvider(
        [
            LLMResponse(
                text="A modelagem dimensional organiza dados em fatos e dimensões [F1].",
                input_tokens=150,
                output_tokens=30,
            )
        ]
    )

    res = await explain_topic(repo, provider, user, "BI: modelagem dimensional")
    assert len(provider.calls) == 1
    messages, _ = provider.calls[0]
    sys_content = messages[0].content
    user_content = messages[1].content

    # Prompt contém instruções de fundamentação
    assert "REGRAS INVIOLÁVEIS DE FUNDAMENTAÇÃO" in sys_content
    # Prompt de usuário contém os blocos [F1]
    assert "[F1]" in user_content
    assert "Aula_BI.pdf" in user_content
    # Resposta pós-processada contém citação legível e Fontes
    assert "(Aula_BI.pdf, Slide 1)" in res.text
    assert "Fontes:" in res.text


def test_invalid_citation_removed_and_valid_preserved() -> None:
    sources_map = {
        "[F1]": (
            "Business Intelligence",
            "Aula02.pdf",
            "Slide 5",
            "Definição correta do conceito de data warehouse e modelagem.",
        ),
    }
    raw = "Definição correta do conceito [F1], mas com referência falsa [F99] inventada."
    processed = post_process_study_answer(raw, sources_map)

    # Citação válida [F1] substituída
    assert "(Aula02.pdf, Slide 5)" in processed
    # Citação inválida [F99] removida
    assert "[F99]" not in processed
    assert "Fontes:" in processed
    assert "Aula02.pdf (Slide 5)" in processed


def test_sanitize_answer_urls_keeps_only_context_links() -> None:
    context = ["Consulte https://canvas.pucminas.br/courses/1/pages/aula para detalhes."]
    answer = (
        "Veja https://canvas.pucminas.br/courses/1/pages/aula e "
        "[a documentação](https://reactnavigation.org/docs/getting-started)."
    )

    cleaned = sanitize_answer_urls(answer, context)

    assert "https://canvas.pucminas.br/courses/1/pages/aula" in cleaned
    assert "https://reactnavigation.org/docs/getting-started" not in cleaned
    assert "a documentação" in cleaned


def test_grounding_verification_overlap_threshold() -> None:
    sources_map = {
        "[F1]": (
            "Business Intelligence",
            "Aula03.pptx",
            "Slide 30",
            "Cubo multidimensional e vendas",
        ),
    }
    # Frase com afirmações não presentes no chunk (overlap < 0.35)
    raw = "Essa modelagem é comum em data warehouses e data marts criando estruturas [F1]."
    processed = post_process_study_answer(raw, sources_map, min_overlap=0.35)

    assert "(fonte não confirmada)" in processed
    assert "(Aula03.pptx, Slide 30)" not in processed
    # Fonte não sustentou nenhuma frase, então não aparece na lista de Fontes
    assert "Fontes:" not in processed


def test_coverage_weak_or_none_triggers_disclaimer_rule() -> None:
    prompt_strong = build_study_system_prompt("strong")
    assert "ATENÇÃO COBERTURA FRACA/NULA" not in prompt_strong

    prompt_weak = build_study_system_prompt("weak")
    assert "ATENÇÃO COBERTURA FRACA/NULA" in prompt_weak
    assert "Não encontrei" in prompt_weak
    assert "Complemento (fora do material):" in prompt_weak

    prompt_none = build_study_system_prompt("none")
    assert "ATENÇÃO COBERTURA FRACA/NULA" in prompt_none


async def test_summarize_long_file_requires_range(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Desenvolvimento Mobile")
    repo.upsert_courses(user.user_id, [c1])

    # Cria arquivo com 45 chunks (> teto padrão de 40)
    chunks = [f"Texto detalhado da página {i} de desenvolvimento mobile." for i in range(1, 46)]
    seed_file_and_chunks(
        repo, user, course_id=1, file_id=20, filename="Apostila_Longa.pdf", chunks_text=chunks
    )

    provider = MockLLMProvider([])
    # Sem faixa: deve recusar e orientar pedir faixa sem gastar LLM
    res = await summarize_file(repo, provider, user, "Mobile", "Apostila_Longa")

    assert len(provider.calls) == 0  # Nenhuma chamada ao LLM
    assert "é extenso (45 slides)" in res.text
    assert "por favor informe a faixa desejada" in res.text


async def test_ambiguity_course_and_file_lists_candidates(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Banco de Dados I")
    c2 = course_factory(id=2, name="Banco de Dados II")
    repo.upsert_courses(user.user_id, [c1, c2])

    provider = MockLLMProvider([])
    res = await explain_topic(repo, provider, user, "modelagem", course_query="Banco")

    assert len(provider.calls) == 0
    assert "Mais de uma matéria corresponde a 'Banco':" in res.text
    assert "Banco de Dados I" in res.text
    assert "Banco de Dados II" in res.text


async def test_summary_cache_avoids_second_llm_call(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Redes")
    repo.upsert_courses(user.user_id, [c1])

    chunks = ["Protocolo TCP garante entrega ordenada.", "Protocolo UDP prioriza velocidade."]
    seed_file_and_chunks(
        repo, user, course_id=1, file_id=30, filename="Redes_Aula01.pdf", chunks_text=chunks
    )

    provider = MockLLMProvider(
        [
            LLMResponse(
                text="Resumo das ideias principais de Redes sobre TCP e UDP.",
                input_tokens=100,
                output_tokens=40,
            )
        ]
    )

    # 1ª chamada: chama o LLM
    res1 = await summarize_file(repo, provider, user, "Redes", "Redes_Aula01")
    assert not res1.from_cache
    assert len(provider.calls) == 1

    # 2ª chamada idêntica: deve retornar do cache imediatamente
    res2 = await summarize_file(repo, provider, user, "Redes", "Redes_Aula01")
    assert res2.from_cache
    assert "[Resumo em cache]" in res2.text
    assert len(provider.calls) == 1  # Continua 1, não chamou a API de novo!


async def test_quiz_invalid_json_retries_then_friendly_error(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Segurança")
    repo.upsert_courses(user.user_id, [c1])
    seed_file_and_chunks(
        repo,
        user,
        course_id=1,
        file_id=40,
        filename="Cripto.pdf",
        chunks_text=["Criptografia simétrica usa a mesma chave para cifrar e decifrar."],
    )

    # Duas respostas com JSON quebrado
    provider = MockLLMProvider(
        [
            LLMResponse(
                text="Aqui está o quiz: não é json válido {", input_tokens=80, output_tokens=20
            ),
            LLMResponse(text="Ainda inválido: [1, 2, 3", input_tokens=90, output_tokens=25),
        ]
    )

    quiz_data, res = await generate_quiz(repo, provider, user, "Segurança", "Criptografia", n=3)

    assert quiz_data is None
    assert len(provider.calls) == 2  # Disparou 1 retry
    assert "Dificuldade temporária" in res.text


async def test_quiz_valid_json_stores_answer_key_and_returns_questions(
    repo, user, course_factory
) -> None:
    c1 = course_factory(id=1, name="Segurança")
    repo.upsert_courses(user.user_id, [c1])
    seed_file_and_chunks(
        repo,
        user,
        course_id=1,
        file_id=40,
        filename="Cripto.pdf",
        chunks_text=["Criptografia simétrica usa a mesma chave para cifrar e decifrar."],
    )

    valid_json = """
    {
      "topic": "Criptografia",
      "course": "Segurança",
      "questions": [
        {
          "id": 1,
          "type": "multiple_choice",
          "question": "O que caracteriza a criptografia simétrica?",
          "options": [
            "A) Uso de chaves pública e privada",
            "B) Uso da mesma chave para cifrar e decifrar",
            "C) Ausência total de chaves",
            "D) Cifragem apenas de hashes"
          ],
          "correct_answer": "B) Uso da mesma chave para cifrar e decifrar",
          "explanation": "Na criptografia simétrica, a mesma chave secreta é compartilhada.",
          "source": "Cripto.pdf (Slide 1)"
        }
      ]
    }
    """
    provider = MockLLMProvider([LLMResponse(text=valid_json, input_tokens=120, output_tokens=90)])

    quiz_data, res = await generate_quiz(repo, provider, user, "Segurança", "Criptografia", n=1)

    assert quiz_data is not None
    assert len(quiz_data.questions) == 1
    # Perguntas enviadas para o aluno não contêm a resposta correta
    assert "Questão 1" in res.text
    assert "Resposta correta" not in res.text
    assert "/gabarito" in res.text

    # Gabarito recuperável sob demanda
    key_text = get_quiz_answer_key(user.user_id)
    assert "Gabarito e Comentários: Criptografia" in key_text
    assert "B) Uso da mesma chave para cifrar e decifrar" in key_text
    assert "Cripto.pdf (Slide 1)" in key_text


def test_split_text_long_message() -> None:
    short_text = "Mensagem curta."
    assert split_text(short_text, limit=100) == [short_text]

    # Texto com 350 caracteres dividido em blocos com limite 100
    long_text = ("Linha de teste para divisão de mensagens longas no Telegram. " * 6).strip()
    pieces = split_text(long_text, limit=100)
    assert len(pieces) > 1
    for p in pieces:
        assert len(p) <= 100
    # O conteúdo reconstruído é preservado
    assert " ".join(pieces).replace("  ", " ") == long_text.replace("  ", " ")


def test_context_token_budget_respected(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Algoritmos")
    repo.upsert_courses(user.user_id, [c1])

    # Chunks de 800 caracteres (~200 tokens cada)
    large_chunks = [f"Bloco {i}: " + ("x" * 800) for i in range(1, 10)]
    seed_file_and_chunks(
        repo, user, course_id=1, file_id=50, filename="Algoritmos.pdf", chunks_text=large_chunks
    )

    # Orçamento apertado: 500 tokens (só cabe ~2 blocos)
    pack = build_context(repo, user.user_id, "Bloco", course_id=1, k=10, max_tokens=500)
    assert pack.total_tokens <= 500
    assert len(pack.blocks) <= 2


def test_task_has_body_detection() -> None:
    header_only = "# Tarefa 1\nPrazo: 2026-11-20\nPontos: 10\nTipos de entrega: online_upload"
    assert not _task_has_body(header_only)

    with_body = (
        "# Tarefa 1\nPrazo: 2026-11-20\nPontos: 10\n"
        "Desenvolver um aplicativo em React Native com duas telas."
    )
    assert _task_has_body(with_body)


def test_grounding_overlap_calculation() -> None:
    sentence = "O modelo relacional organiza dados em tabelas com chaves primárias."
    chunk = "Apresentamos tabelas relacionais organizadas com dados e chaves primárias."
    overlap = calculate_grounding_overlap(sentence, chunk)
    assert overlap >= 0.50

    unrelated_chunk = "A física quântica descreve partículas subatômicas e funções de onda."
    assert calculate_grounding_overlap(sentence, unrelated_chunk) < 0.20


def test_jaccard_similarity_quiz_redundancy() -> None:
    ans1 = extract_content_stems("Chave simétrica compartilhada")
    ans2 = extract_content_stems("Chave simétrica compartilhada previamente")
    assert _jaccard_similarity(ans1, ans2) > 0.60

    ans3 = extract_content_stems("Chave assimétrica pública e privada")
    assert _jaccard_similarity(ans1, ans3) < 0.30
    assert _jaccard_similarity(set(), set()) == 0.0
