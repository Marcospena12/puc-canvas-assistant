from __future__ import annotations

import hashlib
from typing import Any

from canvas_assistant.knowledge.search import (
    get_neighbors,
    prepare_fts_query,
    search,
    stem_prefix_pt,
)


def seed_corpus_chunk(
    repo,
    user,
    course_id: int,
    file_id: int,
    filename: str,
    chunk_index: int,
    location_label: str,
    text: str,
    sha256: str = "sha_corpus_1",
) -> None:
    # Garante que o arquivo existe na tabela file_sync_state para que o join funcione
    repo.upsert_file_sync_states(
        user.user_id,
        [
            {
                "file_id": file_id,
                "course_id": course_id,
                "display_name": filename,
                "filename": filename,
                "content_type": "application/pdf",
                "size": 1024,
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
        sha256, status="extraido", total_chunks=1, total_characters=len(text)
    )

    thash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    chunks = [
        {
            "chunk_index": chunk_index,
            "location_type": "pagina",
            "location_label": location_label,
            "location_start": chunk_index,
            "location_end": chunk_index,
            "text": text,
            "text_hash": thash,
            "char_count": len(text),
            "token_estimate": len(text) // 4,
        }
    ]
    repo.replace_file_chunks(user.user_id, course_id, file_id, sha256, chunks)


def test_stem_prefix_pt() -> None:
    assert stem_prefix_pt("dimensões") == '"dimens"*'
    assert stem_prefix_pt("dimensão") == '"dimens"*'
    assert stem_prefix_pt("tabelas") == '"tabel"*'
    assert stem_prefix_pt("tabela") == '"tabel"*'
    assert stem_prefix_pt("sql") == '"sql"'
    assert stem_prefix_pt("fato") == '"fato"'


def test_prepare_fts_query_cleanliness_and_hostile_inputs() -> None:
    # Acentos removidos, stopwords ignoradas
    and_q, or_q = prepare_fts_query("a avaliação de banco de dados")
    assert '"avaliac"*' in and_q
    assert '"banc"*' in and_q
    assert '"dad"*' in and_q

    # Entradas hostis não quebram e não geram sintaxe FTS inválida
    assert prepare_fts_query("") == ("", "")
    assert prepare_fts_query("   ") == ("", "")
    assert prepare_fts_query('"""') == ("", "")
    assert prepare_fts_query("AND OR NOT NEAR ( ) * : -") == ("", "")
    assert prepare_fts_query("de da o a que em para com") == ("", "")

    # Aspas e parênteses misturados
    safe_and, _ = prepare_fts_query('("modelagem" OR NOT: *tabela*)')
    assert '"modelag"*' in safe_and
    assert '"tabel"*' in safe_and


def test_search_accents_and_inflections(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Banco de Dados")
    repo.upsert_courses(user.user_id, [c1])

    text_content = (
        "Nesta aula abordamos a dimensão de tempo e a construção de tabelas "
        "dimensionais para Data Warehouse."
    )
    seed_corpus_chunk(
        repo,
        user,
        course_id=1,
        file_id=10,
        filename="Aula_DW.pdf",
        chunk_index=1,
        location_label="Página 1",
        text=text_content,
    )

    # 1. Busca com plural/flexão: 'dimensões' encontra 'dimensão'
    res_plural = search(repo, user.user_id, "dimensões", k=5)
    assert len(res_plural) == 1
    assert res_plural[0].file_id == 10
    assert "dimensão" in res_plural[0].snippet.lower()

    # 2. Busca sem acento: 'dimensao' encontra 'dimensão'
    res_sem_acento = search(repo, user.user_id, "dimensao", k=5)
    assert len(res_sem_acento) == 1

    # 3. Busca singular: 'tabela' encontra 'tabelas'
    res_sing = search(repo, user.user_id, "tabela", k=5)
    assert len(res_sing) == 1

    # 4. Termo ausente
    res_ausente = search(repo, user.user_id, "termo_inexistente_xpto", k=5)
    assert res_ausente == []


def test_search_filename_boost_ranking(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Banco de Dados")
    repo.upsert_courses(user.user_id, [c1])

    # Arquivo A tem 'Modelagem' no nome do arquivo
    seed_corpus_chunk(
        repo,
        user,
        course_id=1,
        file_id=10,
        filename="Guia_Modelagem_Dimensional.pdf",
        chunk_index=1,
        location_label="Página 1",
        text="Apresentamos os fundamentos de banco de dados e arquitetura relacional.",
        sha256="sha_a",
    )

    # Arquivo B tem 'Modelagem' apenas uma vez no corpo do texto
    seed_corpus_chunk(
        repo,
        user,
        course_id=1,
        file_id=20,
        filename="Introducao_Geral.pdf",
        chunk_index=1,
        location_label="Página 1",
        text="Vários tópicos serão discutidos durante o semestre, inclusive modelagem básica.",
        sha256="sha_b",
    )

    results = search(repo, user.user_id, "modelagem", k=5)
    assert len(results) == 2
    # Arquivo A deve liderar o ranking por causa do peso maior (5.0) no filename
    assert results[0].file_id == 10
    assert results[1].file_id == 20


def test_search_course_filter_and_deduplication(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Matéria Alfa")
    c2 = course_factory(id=2, name="Matéria Beta")
    repo.upsert_courses(user.user_id, [c1, c2])

    common_text = (
        "Metodologia de pesquisa científica aplicada a engenharia de software e computação."
    )
    shared_sha = "sha_shared_42"

    # Mesmo arquivo compartilhado nas matérias 1 e 2
    seed_corpus_chunk(
        repo,
        user,
        course_id=1,
        file_id=100,
        filename="Metodologia.pdf",
        chunk_index=1,
        location_label="Página 1",
        text=common_text,
        sha256=shared_sha,
    )
    seed_corpus_chunk(
        repo,
        user,
        course_id=2,
        file_id=200,
        filename="Metodologia_Comp.pdf",
        chunk_index=1,
        location_label="Página 1",
        text=common_text,
        sha256=shared_sha,
    )

    # 1. Busca global: colapsa duplicatas e aponta ambas as matérias
    global_res = search(repo, user.user_id, "metodologia científica", k=5)
    assert len(global_res) == 1
    assert set(global_res[0].associated_courses) == {"Matéria Alfa", "Matéria Beta"}

    # 2. Busca filtrada por matéria
    filtered_res = search(repo, user.user_id, "metodologia científica", course_id=1, k=5)
    assert len(filtered_res) == 1
    assert filtered_res[0].course_id == 1


def test_reindex_consistency_and_triggers(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Banco de Dados")
    repo.upsert_courses(user.user_id, [c1])

    seed_corpus_chunk(
        repo,
        user,
        course_id=1,
        file_id=10,
        filename="Aula1.pdf",
        chunk_index=1,
        location_label="Página 1",
        text="Texto versão 1 sobre concorrência e transações ACID.",
        sha256="sha_v1",
    )

    check1 = repo.fts_consistency_check()
    assert check1["chunks_count"] == 1
    assert check1["fts_count"] == 1

    # Atualiza / reextrai o arquivo (apaga chunk antigo e insere novo via trigger)
    seed_corpus_chunk(
        repo,
        user,
        course_id=1,
        file_id=10,
        filename="Aula1.pdf",
        chunk_index=1,
        location_label="Página 1",
        text="Texto versão 2 completamente reescrito sobre isolamento serializável.",
        sha256="sha_v2",
    )

    check2 = repo.fts_consistency_check()
    assert check2["chunks_count"] == 1
    assert check2["fts_count"] == 1

    # Busca termo antigo não encontra mais; busca termo novo encontra
    assert search(repo, user.user_id, "ACID") == []
    res_novo = search(repo, user.user_id, "serializável")
    assert len(res_novo) == 1

    # Teste de reindexação explícita (reindex_fts)
    indexed, total = repo.reindex_fts()
    assert indexed == 1
    assert total == 1

    check3 = repo.fts_consistency_check()
    assert check3["chunks_count"] == 1
    assert check3["fts_count"] == 1


def test_get_neighbors_at_file_boundaries(repo, user, course_factory) -> None:
    c1 = course_factory(id=1, name="Banco de Dados")
    repo.upsert_courses(user.user_id, [c1])

    # Cria arquivo com 3 chunks sequenciais
    sha = "sha_multi"
    repo.upsert_file_sync_states(
        user.user_id,
        [
            {
                "file_id": 50,
                "course_id": 1,
                "display_name": "Apostila.pdf",
                "filename": "Apostila.pdf",
                "content_type": "application/pdf",
                "size": 3000,
                "modified_at": "2026-11-01T12:00:00+00:00",
                "local_path": "1/50_Apostila.pdf",
                "sha256": sha,
                "status": "downloaded",
                "first_seen_at": "2026-11-01T12:00:00+00:00",
                "updated_at": "2026-11-01T12:00:00+00:00",
                "active": 1,
            }
        ],
    )
    repo.save_extracted_content(sha, status="extraido", total_chunks=3, total_characters=300)

    chunks: list[dict[str, Any]] = [
        {
            "chunk_index": 1,
            "location_type": "pagina",
            "location_label": "Página 1",
            "location_start": 1,
            "location_end": 1,
            "text": "Primeiro bloco de introdução.",
            "text_hash": "h1",
            "char_count": 29,
            "token_estimate": 7,
        },
        {
            "chunk_index": 2,
            "location_type": "pagina",
            "location_label": "Página 2",
            "location_start": 2,
            "location_end": 2,
            "text": "Segundo bloco com desenvolvimento do tema.",
            "text_hash": "h2",
            "char_count": 42,
            "token_estimate": 10,
        },
        {
            "chunk_index": 3,
            "location_type": "pagina",
            "location_label": "Página 3",
            "location_start": 3,
            "location_end": 3,
            "text": "Terceiro bloco com conclusões finais.",
            "text_hash": "h3",
            "char_count": 37,
            "token_estimate": 9,
        },
    ]
    repo.replace_file_chunks(user.user_id, 1, 50, sha, chunks)

    all_chunks = repo.get_chunks_by_file(user.user_id, 50)
    c1_id = all_chunks[0]["id"]
    c2_id = all_chunks[1]["id"]
    c3_id = all_chunks[2]["id"]

    # No início (primeiro chunk): vizinho anterior não existe
    n_first = get_neighbors(repo, c1_id, before=1, after=1)
    assert [c["chunk_index"] for c in n_first] == [1, 2]

    # No meio: anterior e posterior presentes
    n_mid = get_neighbors(repo, c2_id, before=1, after=1)
    assert [c["chunk_index"] for c in n_mid] == [1, 2, 3]

    # No fim (último chunk): vizinho posterior não existe
    n_last = get_neighbors(repo, c3_id, before=1, after=1)
    assert [c["chunk_index"] for c in n_last] == [2, 3]
