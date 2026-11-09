from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import docx
import pptx
import pymupdf

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTILINE_RE = re.compile(r"\n{3,}")


def clean_text(text: str) -> str:
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in (text or "").splitlines()]
    compact = "\n".join(lines)
    return _MULTILINE_RE.sub("\n\n", compact).strip()


@dataclass(frozen=True, slots=True)
class DocumentUnit:
    location_type: str  # pagina, slide, secao, etc.
    index_start: int
    index_end: int
    label: str
    text: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    status: str  # extraido, sem_texto, falha
    units: list[DocumentUnit]
    error_message: str | None = None
    total_characters: int = 0
    empty_units_count: int = 0


def extract_pdf(path: Path) -> ExtractionResult:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:
        return ExtractionResult(status="falha", units=[], error_message=f"Erro ao abrir PDF: {exc}")

    try:
        units: list[DocumentUnit] = []
        pages_without_text = 0
        total_pages = len(doc)

        for page_idx in range(total_pages):
            page = doc[page_idx]
            raw_text = page.get_text("text") or ""
            cleaned = clean_text(raw_text)

            # Heurística: menos de 20 caracteres úteis conta como página sem texto
            if len(cleaned) < 20:
                pages_without_text += 1
            else:
                page_num = page_idx + 1
                units.append(
                    DocumentUnit(
                        location_type="pagina",
                        index_start=page_num,
                        index_end=page_num,
                        label=f"Página {page_num}",
                        text=cleaned,
                    )
                )

        if not units or (total_pages > 0 and len(units) == 0):
            msg = f"PDF sem texto detectável em {total_pages} página(s) (provável imagem escaneada)"
            return ExtractionResult(
                status="sem_texto",
                units=[],
                error_message=msg,
                empty_units_count=pages_without_text,
            )

        total_chars = sum(len(u.text) for u in units)
        return ExtractionResult(
            status="extraido",
            units=units,
            total_characters=total_chars,
            empty_units_count=pages_without_text,
        )
    except Exception as exc:
        return ExtractionResult(
            status="falha", units=[], error_message=f"Erro ao ler páginas do PDF: {exc}"
        )
    finally:
        doc.close()


def _collect_shape_text(shape) -> list[str]:
    texts: list[str] = []
    if shape.has_text_frame and shape.text_frame:
        for paragraph in shape.text_frame.paragraphs:
            p_text = clean_text(paragraph.text)
            if p_text:
                texts.append(p_text)
    elif shape.has_table and shape.table:
        for row in shape.table.rows:
            row_cells = [clean_text(cell.text) for cell in row.cells if clean_text(cell.text)]
            if row_cells:
                texts.append(" | ".join(row_cells))
    elif shape.shape_type == pptx.enum.shapes.MSO_SHAPE_TYPE.GROUP:
        for sub_shape in shape.shapes:
            texts.extend(_collect_shape_text(sub_shape))
    return texts


def extract_pptx(path: Path) -> ExtractionResult:
    try:
        prs = pptx.Presentation(path)
    except Exception as exc:
        return ExtractionResult(
            status="falha", units=[], error_message=f"Erro ao abrir PPTX: {exc}"
        )

    try:
        units: list[DocumentUnit] = []
        empty_slides_count = 0
        for slide_idx, slide in enumerate(prs.slides):
            slide_num = slide_idx + 1
            slide_lines: list[str] = []

            for shape in slide.shapes:
                slide_lines.extend(_collect_shape_text(shape))

            if slide.has_notes_slide and slide.notes_slide:
                notes_tf = slide.notes_slide.notes_text_frame
                if notes_tf:
                    notes_text = clean_text(notes_tf.text)
                    if notes_text:
                        slide_lines.append(f"[Notas do orador: {notes_text}]")

            cleaned = clean_text("\n".join(slide_lines))
            if cleaned:
                units.append(
                    DocumentUnit(
                        location_type="slide",
                        index_start=slide_num,
                        index_end=slide_num,
                        label=f"Slide {slide_num}",
                        text=cleaned,
                    )
                )
            else:
                empty_slides_count += 1

        if not units:
            return ExtractionResult(
                status="sem_texto",
                units=[],
                error_message="Apresentação PPTX sem texto nos slides",
                empty_units_count=empty_slides_count,
            )

        total_chars = sum(len(u.text) for u in units)
        return ExtractionResult(
            status="extraido",
            units=units,
            total_characters=total_chars,
            empty_units_count=empty_slides_count,
        )
    except Exception as exc:
        return ExtractionResult(
            status="falha", units=[], error_message=f"Erro ao extrair slides do PPTX: {exc}"
        )


def extract_docx(path: Path) -> ExtractionResult:
    try:
        doc = docx.Document(path)
    except Exception as exc:
        return ExtractionResult(
            status="falha", units=[], error_message=f"Erro ao abrir DOCX: {exc}"
        )

    try:
        units: list[DocumentUnit] = []
        current_section = 1
        current_heading = "Início"
        section_lines: list[str] = []

        def flush_section():
            nonlocal current_section, current_heading, section_lines
            content = clean_text("\n".join(section_lines))
            if content:
                label = (
                    f"Seção {current_section}: {current_heading}"
                    if current_heading != "Início"
                    else f"Seção {current_section}"
                )
                units.append(
                    DocumentUnit(
                        location_type="secao",
                        index_start=current_section,
                        index_end=current_section,
                        label=label,
                        text=content,
                    )
                )
                current_section += 1
            section_lines = []

        for element in doc.element.body:
            if element.tag.endswith("p"):
                para = docx.text.paragraph.Paragraph(element, doc)
                p_text = clean_text(para.text)
                if not p_text:
                    continue
                if para.style and para.style.name and para.style.name.startswith("Heading"):
                    flush_section()
                    current_heading = p_text[:60]
                section_lines.append(p_text)
            elif element.tag.endswith("tbl"):
                table = docx.table.Table(element, doc)
                for row in table.rows:
                    row_cells = [
                        clean_text(cell.text) for cell in row.cells if clean_text(cell.text)
                    ]
                    if row_cells:
                        section_lines.append(" | ".join(row_cells))

        flush_section()

        if not units:
            return ExtractionResult(
                status="sem_texto",
                units=[],
                error_message="Documento DOCX sem texto legível",
            )

        total_chars = sum(len(u.text) for u in units)
        return ExtractionResult(status="extraido", units=units, total_characters=total_chars)
    except Exception as exc:
        return ExtractionResult(
            status="falha", units=[], error_message=f"Erro ao extrair DOCX: {exc}"
        )


def extract_text_file(path: Path) -> ExtractionResult:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return ExtractionResult(
            status="falha", units=[], error_message=f"Erro ao ler arquivo texto: {exc}"
        )

    cleaned = clean_text(content)
    if not cleaned:
        return ExtractionResult(
            status="sem_texto",
            units=[],
            error_message="Arquivo de texto vazio",
        )

    unit = DocumentUnit(
        location_type="documento",
        index_start=1,
        index_end=1,
        label="Documento completo",
        text=cleaned,
    )
    return ExtractionResult(status="extraido", units=[unit], total_characters=len(cleaned))


def extract_file(path: Path) -> ExtractionResult:
    if not path.is_file():
        return ExtractionResult(
            status="falha", units=[], error_message=f"Arquivo não encontrado: {path}"
        )

    suffix = path.suffix.lower().lstrip(".")
    if suffix == "pdf":
        return extract_pdf(path)
    if suffix in ("pptx", "ppt"):
        return extract_pptx(path)
    if suffix in ("docx", "doc"):
        return extract_docx(path)
    if suffix in ("txt", "md"):
        return extract_text_file(path)

    return ExtractionResult(
        status="falha",
        units=[],
        error_message=f"Formato .{suffix} não suportado para extração",
    )
