from __future__ import annotations

import hashlib
from pathlib import Path

import docx
import pptx
import pymupdf

from canvas_assistant.knowledge.chunking import build_chunks
from canvas_assistant.knowledge.extraction import ExtractionPipeline
from canvas_assistant.knowledge.extractors import (
    clean_text,
    extract_docx,
    extract_file,
    extract_pdf,
    extract_pptx,
    extract_text_file,
)


def create_sample_pdf(path: Path, text_pages: list[str]) -> None:
    doc = pymupdf.open()
    for text in text_pages:
        page = doc.new_page()
        if text:
            page.insert_text((50, 50), text, fontsize=12)
    doc.save(path)
    doc.close()


def create_sample_pptx(path: Path, slides_content: list[tuple[str, str | None]]) -> None:
    prs = pptx.Presentation()
    blank_layout = prs.slide_layouts[6]
    for body_text, notes in slides_content:
        slide = prs.slides.add_slide(blank_layout)
        tx_box = slide.shapes.add_textbox(100, 100, 400, 200)
        tf = tx_box.text_frame
        tf.text = body_text
        if notes:
            notes_slide = slide.notes_slide
            notes_tf = notes_slide.notes_text_frame
            notes_tf.text = notes
    prs.save(path)


def create_sample_docx(path: Path, paragraphs_and_headings: list[tuple[str, bool]]) -> None:
    doc = docx.Document()
    for text, is_heading in paragraphs_and_headings:
        if is_heading:
            doc.add_heading(text, level=1)
        else:
            doc.add_paragraph(text)
    doc.save(path)


def test_clean_text() -> None:
    raw = "  Olá    mundo!\n\n\n\nPróxima   linha   com  espaços.  \n"
    assert clean_text(raw) == "Olá mundo!\n\nPróxima linha com espaços."


def test_extract_pdf_with_text_and_empty_pages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "teste.pdf"
    create_sample_pdf(
        pdf_path,
        [
            "Primeira página do documento com conteúdo textual suficiente para teste.",
            "",  # página vazia
            "Terceira página com mais explicações detalhadas sobre banco de dados relacional.",
        ],
    )

    res = extract_pdf(pdf_path)
    assert res.status == "extraido"
    assert len(res.units) == 2
    assert res.units[0].label == "Página 1"
    assert "Primeira página" in res.units[0].text
    assert res.units[1].label == "Página 3"
    assert "Terceira página" in res.units[1].text


def test_extract_pdf_without_text(tmp_path: Path) -> None:
    pdf_path = tmp_path / "vazio.pdf"
    create_sample_pdf(pdf_path, ["", "  ", "short"])

    res = extract_pdf(pdf_path)
    assert res.status == "sem_texto"
    assert res.units == []


def test_extract_pptx_with_shapes_and_notes(tmp_path: Path) -> None:
    pptx_path = tmp_path / "aula.pptx"
    create_sample_pptx(
        pptx_path,
        [
            ("Introdução ao Eixo Temático de Algoritmos", "Lembrar de falar sobre a ementa"),
            ("Estruturas de Dados Básicas: Filas e Pilhas", None),
        ],
    )

    res = extract_pptx(pptx_path)
    assert res.status == "extraido"
    assert len(res.units) == 2
    assert res.units[0].label == "Slide 1"
    assert "Lembrar de falar sobre a ementa" in res.units[0].text
    assert res.units[1].label == "Slide 2"
    assert "Estruturas de Dados" in res.units[1].text


def test_extract_docx_with_headings(tmp_path: Path) -> None:
    docx_path = tmp_path / "trabalho.docx"
    create_sample_docx(
        docx_path,
        [
            ("Introdução Teórica", True),
            ("Este trabalho aborda sistemas distribuídos e concorrência.", False),
            ("Metodologia Experimental", True),
            ("Os experimentos foram executados em ambiente de teste com carga sintética.", False),
        ],
    )

    res = extract_docx(docx_path)
    assert res.status == "extraido"
    assert len(res.units) == 2
    assert "Introdução Teórica" in res.units[0].label
    assert "sistemas distribuídos" in res.units[0].text
    assert "Metodologia Experimental" in res.units[1].label
    assert "experimentos" in res.units[1].text


def test_extract_text_file(tmp_path: Path) -> None:
    txt_path = tmp_path / "notas.md"
    txt_path.write_text("# Anotações de Aula\n\nConteúdo explicativo.", encoding="utf-8")

    res = extract_text_file(txt_path)
    assert res.status == "extraido"
    assert len(res.units) == 1
    assert "Anotações de Aula" in res.units[0].text


def test_extract_file_unsupported_format(tmp_path: Path) -> None:
    img_path = tmp_path / "diagrama.png"
    img_path.write_bytes(b"\x89PNG\r\n\x1a\n")

    res = extract_file(img_path)
    assert res.status == "falha"
    assert "não suportado" in (res.error_message or "")


def test_chunking_slides_grouping(tmp_path: Path) -> None:
    pptx_path = tmp_path / "micro.pptx"
    create_sample_pptx(
        pptx_path,
        [
            ("Slide Curto 1", None),
            ("Slide Curto 2", None),
            ("Slide Curto 3", None),
        ],
    )
    res = extract_pptx(pptx_path)
    chunks = build_chunks(res.units)

    assert len(chunks) == 1
    assert chunks[0].location_type == "slide"
    assert chunks[0].location_label == "Slides 1-3"
    assert "Slide Curto 1" in chunks[0].text
    assert "Slide Curto 3" in chunks[0].text


def test_chunking_long_page_splitting(tmp_path: Path) -> None:
    long_text = (
        "Este é um parágrafo bastante extenso e explicativo sobre arquitetura de software.\n\n" * 60
    )
    txt_path = tmp_path / "longo.txt"
    txt_path.write_text(long_text, encoding="utf-8")

    res = extract_text_file(txt_path)
    chunks = build_chunks(res.units)

    assert len(chunks) > 1
    assert "Documento completo (parte 1/" in chunks[0].location_label
    assert all(c.char_count > 0 for c in chunks)


def test_extraction_pipeline_incremental_and_dedupe(
    tmp_path: Path, repo, user, course_factory
) -> None:
    files_dir = tmp_path / "files"
    files_dir.mkdir()

    course1 = course_factory(id=1, name="Matéria 1")
    course2 = course_factory(id=2, name="Matéria 2")
    repo.upsert_courses(user.user_id, [course1, course2])

    pdf_rel = Path("1") / "10_aula.pdf"
    pdf_full = files_dir / pdf_rel
    pdf_full.parent.mkdir(parents=True, exist_ok=True)
    create_sample_pdf(pdf_full, ["Texto da aula 1 sobre grafos e árvores binárias de busca."])

    sha = hashlib.sha256(pdf_full.read_bytes()).hexdigest()

    # Registra o mesmo arquivo nas duas matérias (mesmo sha256)
    rows = [
        {
            "file_id": 10,
            "course_id": 1,
            "display_name": "Aula Grafos.pdf",
            "filename": "aula.pdf",
            "content_type": "application/pdf",
            "size": pdf_full.stat().st_size,
            "modified_at": "2026-11-01T12:00:00+00:00",
            "local_path": pdf_rel.as_posix(),
            "sha256": sha,
            "status": "downloaded",
            "first_seen_at": "2026-11-01T12:00:00+00:00",
            "updated_at": "2026-11-01T12:00:00+00:00",
            "active": 1,
        },
        {
            "file_id": 20,
            "course_id": 2,
            "display_name": "Aula Grafos Compartilhada.pdf",
            "filename": "aula_comp.pdf",
            "content_type": "application/pdf",
            "size": pdf_full.stat().st_size,
            "modified_at": "2026-11-01T12:00:00+00:00",
            "local_path": pdf_rel.as_posix(),
            "sha256": sha,
            "status": "downloaded",
            "first_seen_at": "2026-11-01T12:00:00+00:00",
            "updated_at": "2026-11-01T12:00:00+00:00",
            "active": 1,
        },
    ]
    repo.upsert_file_sync_states(user.user_id, rows)

    pipeline = ExtractionPipeline(repo, user, files_dir)

    # 1ª execução: extrai o primeiro e reaproveita o conteúdo no segundo pelo mesmo sha256
    run1 = pipeline.run()
    assert run1.processed_files == 2
    assert run1.reused_content == 1
    assert run1.extracted == 2

    chunks_f1 = repo.get_chunks_by_file(user.user_id, 10)
    chunks_f2 = repo.get_chunks_by_file(user.user_id, 20)
    assert len(chunks_f1) == 1
    assert len(chunks_f2) == 1
    assert chunks_f1[0]["text"] == chunks_f2[0]["text"]
    assert chunks_f1[0]["course_id"] == 1
    assert chunks_f2[0]["course_id"] == 2

    # 2ª execução (sem force): nada para reprocessar
    run2 = pipeline.run(force_all=False)
    assert run2.processed_files == 0
    assert run2.skipped_files == 2

    # Estatísticas de conhecimento
    k_stats = repo.knowledge_stats(user.user_id)
    assert k_stats["status_counts"].get("extraido") == 2
    assert k_stats["total_chunks"] == 2
