from __future__ import annotations

import contextlib
import html
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

_CANVAS_FILE_RE = re.compile(r"(?:/courses/\d+)?/files/(\d+)")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_MULTILINE_RE = re.compile(r"\n{3,}")
_LTI_TITLE_RE = re.compile(r"[?&](?:title|resource_link_title)=([^&]+)", re.IGNORECASE)


def _extract_lti_title(src: str) -> str:
    """Extrai o título de uma URL LTI (parâmetro title/resource_link_title)."""
    match = _LTI_TITLE_RE.search(src)
    if match:
        try:
            from urllib.parse import unquote

            return unquote(match.group(1)).replace("+", " ").strip()
        except Exception:
            return ""
    return ""


def _is_video_tag(tag_name: str, src: str) -> bool:
    """Identifica tags que contêm vídeo (iframe LTI de videoaula, <video>, embed de vídeo)."""
    if tag_name == "video":
        return True
    if tag_name in ("iframe", "embed", "object"):
        return any(
            keyword in src.lower()
            for keyword in (
                "instructuremedia",
                "youtube",
                "vimeo",
                "youtu.be",
                ".mp4",
                "lti/launch",
            )
        )
    return False


@dataclass(frozen=True, slots=True)
class CanvasHtmlResult:
    text: str
    canvas_file_ids: set[int] = field(default_factory=set)
    external_links: list[tuple[str, str]] = field(default_factory=list)
    images_without_alt: int = 0
    media: list[tuple[str, str, str]] = field(default_factory=list)


def clean_canvas_html(raw_html: str | None) -> CanvasHtmlResult:
    """Converte HTML do Canvas em texto estruturado preservando títulos, listas e tabelas.

    - Remove scripts, styles e tags não semânticas.
    - Converte tabelas em células separadas por ' | '.
    - Preserva alt de imagens e conta imagens sem alt.
    - Detecta links para arquivos do Canvas (/files/ID).
    - Agrupa links externos ao final sob 'Links:'.
    """
    if not raw_html or not raw_html.strip():
        return CanvasHtmlResult(text="")

    soup = BeautifulSoup(raw_html, "html.parser")

    canvas_file_ids: set[int] = set()
    external_links: list[tuple[str, str]] = []
    seen_links: set[str] = set()
    images_without_alt = 0
    media: list[tuple[str, str, str]] = []

    # Captura mídia incorporada antes de remover os elementos não textuais.
    for tag in soup.find_all(["iframe", "video", "audio", "object", "embed"]):
        src = (
            tag.get("src")
            or tag.get("data")
            or (tag.find("source").get("src") if tag.find("source") else "")
            or ""
        )
        src = str(src).strip()
        label = str(tag.get("title") or tag.get("aria-label") or tag.name).strip()
        if src:
            # Extrai título de URL LTI se não houver title/aria-label
            if not label or label == tag.name:
                lti_title = _extract_lti_title(src)
                if lti_title:
                    label = lti_title
            media.append((tag.name, src, label))
            if _is_video_tag(tag.name, src) and label and label != tag.name:
                tag.replace_with(soup.new_string(f"\n[Vídeo: {label}] {src}\n"))
            else:
                tag.replace_with(soup.new_string(f"\n[Mídia: {label}] {src}\n"))
        else:
            tag.decompose()

    # Remove elementos sem conteúdo textual útil
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()

    # Coleta links e imagens antes da conversão de nós
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        link_text = " ".join(a.get_text().split()).strip() or "Link"
        file_match = _CANVAS_FILE_RE.search(href)
        if file_match:
            with contextlib.suppress(ValueError):
                canvas_file_ids.add(int(file_match.group(1)))
        elif href.startswith(("http://", "https://")) and href not in seen_links:
            seen_links.add(href)
            external_links.append((link_text, href))

    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        if alt:
            img.replace_with(soup.new_string(f" [Imagem: {alt}] "))
        else:
            images_without_alt += 1
            img.decompose()

    # Formata tabelas: células separadas por ' | ' e quebras de linha por tr
    for table in soup.find_all("table"):
        table_lines: list[str] = []
        for tr in table.find_all("tr"):
            cells = [" ".join(td.get_text().split()).strip() for td in tr.find_all(["td", "th"])]
            row_text = " | ".join(c for c in cells if c)
            if row_text:
                table_lines.append(row_text)
        if table_lines:
            table_block = "\n" + "\n".join(table_lines) + "\n"
            table.replace_with(soup.new_string(table_block))
        else:
            table.decompose()

    # Formata cabeçalhos
    for h in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"]):
        h_text = " ".join(h.get_text().split()).strip()
        if h_text:
            h.replace_with(soup.new_string(f"\n\n# {h_text}\n\n"))
        else:
            h.decompose()

    # Formata itens de lista
    for li in soup.find_all("li"):
        li_text = " ".join(li.get_text().split()).strip()
        if li_text:
            li.replace_with(soup.new_string(f"\n• {li_text}"))
        else:
            li.decompose()

    for p in soup.find_all(["p", "div", "section", "article"]):
        p.insert_before(soup.new_string("\n"))
        p.insert_after(soup.new_string("\n"))

    raw_text = soup.get_text()
    decoded = html.unescape(raw_text)

    # Limpa espaçamento
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in decoded.splitlines()]
    compact = "\n".join(lines)
    cleaned = _MULTILINE_RE.sub("\n\n", compact).strip()

    # Anexa links externos no final se existirem
    if external_links:
        link_items = [f"- {label}: {url}" for label, url in external_links[:10]]
        cleaned = f"{cleaned}\n\nLinks:\n" + "\n".join(link_items)

    return CanvasHtmlResult(
        text=cleaned,
        canvas_file_ids=canvas_file_ids,
        external_links=external_links,
        images_without_alt=images_without_alt,
        media=media,
    )
