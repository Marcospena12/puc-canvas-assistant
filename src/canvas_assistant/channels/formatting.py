from __future__ import annotations

import re
from html import escape

from ..urls import normalize_url

_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_HEADING_RE = re.compile(r"(?m)^#{1,6}\s*")
_TOKEN_RE = re.compile(r"\x00T(\d+)\x00")
_SAFE_TAG_RE = re.compile(r"(</?(?:b|i|code|a)(?:\s+href=\"[^\"]*\")?>)")


def format_llm_for_telegram(text: str) -> str:
    """Prepara o texto de resposta do LLM para exibição rica e limpa no Telegram."""
    val = text or ""
    # Remove linhas horizontais do Markdown
    val = re.sub(r"(?m)^[-*_]{3,}\s*$", "", val)
    # Transforma cabeçalhos Markdown (#, ##, ###) em negrito
    val = re.sub(r"(?m)^#{1,6}\s*(.+?)\s*$", r"**\1**", val)
    # Substitui marcadores de lista por bullet limpo •
    val = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", val)
    return re.sub(r"\n{3,}", "\n\n", val).strip()


def to_telegram_html(text: str, allowed_urls: set[str] | None = None) -> str:
    value = text or ""
    tokens: list[tuple[str, str]] = []

    def store(kind: str, content: str) -> str:
        index = len(tokens)
        tokens.append((kind, content))
        return f"\x00T{index}\x00"

    def replace_link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if allowed_urls is not None and normalize_url(url) not in allowed_urls:
            return label
        return store("link", f"{label}\x00{url}")

    # 1. Preserva links Markdown e blocos de código
    value = _MD_LINK_RE.sub(replace_link, value)
    value = _CODE_RE.sub(lambda match: store("code", match.group(1)), value)

    # 2. Preserva tags HTML já seguras e intencionais (<b>, <i>, <code>, <a>)
    value = _SAFE_TAG_RE.sub(lambda match: store("tag", match.group(1)), value)

    # 3. Remove marcadores de cabeçalho Markdown residuais
    value = _HEADING_RE.sub("", value)

    # 4. Escapa com segurança todos os caracteres restantes (<, >, &)
    value = escape(value, quote=False)

    # 5. Aplica negrito e itálico do Markdown
    value = _BOLD_RE.sub(r"<b>\1</b>", value)
    value = _ITALIC_RE.sub(r"<i>\1</i>", value)

    def restore(match: re.Match[str]) -> str:
        kind, content = tokens[int(match.group(1))]
        if kind == "code":
            return f"<code>{escape(content, quote=False)}</code>"
        if kind == "tag":
            return content
        label, url = content.split("\x00", 1)
        return f'<a href="{escape(url, quote=True)}">{escape(label, quote=False)}</a>'

    return _TOKEN_RE.sub(restore, value).strip()
