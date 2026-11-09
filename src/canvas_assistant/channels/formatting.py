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

    value = _MD_LINK_RE.sub(replace_link, value)
    value = _CODE_RE.sub(lambda match: store("code", match.group(1)), value)
    value = _HEADING_RE.sub("", value)
    value = escape(value, quote=False)
    value = _BOLD_RE.sub(r"<b>\1</b>", value)
    value = _ITALIC_RE.sub(r"<i>\1</i>", value)

    def restore(match: re.Match[str]) -> str:
        kind, content = tokens[int(match.group(1))]
        if kind == "code":
            return f"<code>{escape(content, quote=False)}</code>"
        label, url = content.split("\x00", 1)
        return f'<a href="{escape(url, quote=True)}">{escape(label, quote=False)}</a>'

    return _TOKEN_RE.sub(restore, value).strip()
