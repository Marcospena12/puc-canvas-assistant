from __future__ import annotations

import re
from typing import Any

_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://[^\s<>\[\]]+")
_TRAILING = ".,;:!?)]}'\""
_RESOURCE_ID_RE = re.compile(r"/(?:assignments|files|quizzes|pages)/(\d+)")


def normalize_url(url: str) -> str:
    return url.strip().rstrip(_TRAILING).rstrip("/")


def extract_urls(text: str) -> set[str]:
    urls: set[str] = set()
    for match in _MD_LINK_RE.finditer(text or ""):
        urls.add(normalize_url(match.group(2)))
    for match in _BARE_URL_RE.finditer(text or ""):
        urls.add(normalize_url(match.group(0)))
    return {url for url in urls if url}


def urls_from_value(value: Any) -> set[str]:
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            if node.startswith(("http://", "https://")):
                found.add(normalize_url(node))
        elif isinstance(node, dict):
            for item in node.values():
                walk(item)
        elif isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(value)
    return found


def resource_ids_from_urls(urls: set[str]) -> set[str]:
    ids: set[str] = set()
    for url in urls:
        for match in _RESOURCE_ID_RE.finditer(url):
            ids.add(match.group(1))
    return ids


def resource_ids_from_text(text: str) -> set[str]:
    return {match.group(1) for match in _RESOURCE_ID_RE.finditer(text or "")}
