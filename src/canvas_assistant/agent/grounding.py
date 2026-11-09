from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from ..textutil import short_course_name
from ..urls import (
    extract_urls,
    resource_ids_from_text,
    resource_ids_from_urls,
    urls_from_value,
)
from .context import Evidence

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ToolObservation:
    name: str
    arguments: dict[str, Any]
    raw: str
    data: dict[str, Any] | None
    urls: frozenset[str]


def parse_tool_result(name: str, arguments: dict[str, Any] | None, raw: str) -> ToolObservation:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        parsed = None
    data = parsed if isinstance(parsed, dict) else None
    return ToolObservation(
        name=name,
        arguments=dict(arguments or {}),
        raw=raw or "",
        data=data,
        urls=frozenset(urls_from_value(data)),
    )


def build_evidence(observations: list[ToolObservation], *, limit: int = 3000) -> Evidence:
    if not observations:
        return Evidence(text="", urls=frozenset())

    parts: list[str] = []
    urls: set[str] = set()
    selected = [obs for obs in observations if obs.data is not None][-3:]
    for obs in selected:
        payload = json.dumps(obs.data, ensure_ascii=False)
        if len(payload) > 1500:
            payload = payload[:1500] + "…"
        args = json.dumps(obs.arguments, ensure_ascii=False)
        parts.append(f"- {obs.name} {args}: {payload}")
        urls.update(obs.urls)

    text = "\n".join(parts)
    if len(text) > limit:
        text = text[:limit] + "…"
    return Evidence(text=text, urls=frozenset(urls))


def validate_answer(answer: str, allowed_urls: frozenset[str] | set[str]) -> set[str]:
    allowed = set(allowed_urls)
    unknown = {url for url in extract_urls(answer) if url not in allowed}
    allowed_ids = resource_ids_from_urls(allowed)
    for resource_id in resource_ids_from_text(answer):
        if resource_id not in allowed_ids:
            unknown.add(f"id {resource_id}")
    return unknown


def correction_message(unknown: set[str]) -> str:
    shown = ", ".join(sorted(unknown)[:6])
    return (
        f"A resposta anterior citou dados não confirmados: {shown}.\n"
        "Refaça a resposta usando SOMENTE itens, ids, prazos e links que aparecem "
        "nos resultados das ferramentas ou no bloco DADOS ATUAIS.\n"
        "Se não houver mais itens, diga claramente que não há mais nada. "
        "Não invente títulos, notas, prazos, ids ou links."
    )


def _line(parts: list[Any]) -> str:
    return " | ".join(str(part) for part in parts if part not in (None, "", [], {}))


def _link(url: str | None) -> str:
    return f"[abrir]({url})" if url else "sem link"


def _points(value: Any) -> str:
    if value is None:
        return "sem pontuação"
    if isinstance(value, (int, float)):
        return f"{value:g} pts"
    return str(value)


def _task_line(item: dict[str, Any]) -> str:
    return _line(
        [
            item.get("due_at") or "sem prazo",
            short_course_name(item.get("course") or item.get("course_short")),
            item.get("title") or item.get("name"),
            item.get("status"),
            _points(item.get("points")),
            _link(item.get("url") or item.get("html_url")),
        ]
    )


def _format_payload(data: dict[str, Any]) -> str | None:
    items = data.get("items") or data.get("assignments")
    if isinstance(items, list):
        if not items:
            return "Nenhum item encontrado."
        lines = [f"- {_task_line(item)}" for item in items[:20]]
        if len(items) > 20:
            lines.append(f"- ... e mais {len(items) - 20} item(ns).")
        return "\n".join(lines)

    announcements = data.get("announcements")
    if isinstance(announcements, list):
        if not announcements:
            return "Nenhum aviso encontrado."
        lines = [
            "- "
            + _line(
                [
                    item.get("posted_at"),
                    short_course_name(item.get("course")),
                    item.get("title"),
                    _link(item.get("url")),
                ]
            )
            for item in announcements[:20]
        ]
        if len(announcements) > 20:
            lines.append(f"- ... e mais {len(announcements) - 20} aviso(s).")
        return "\n".join(lines)

    files = data.get("files")
    if isinstance(files, list):
        if not files:
            return "Nenhum arquivo encontrado."
        lines = [
            "- "
            + _line(
                [
                    item.get("name"),
                    f"({short_course_name(item.get('course'))})",
                    _link(item.get("url")),
                ]
            )
            for item in files[:20]
        ]
        if len(files) > 20:
            lines.append(f"- ... e mais {len(files) - 20} arquivo(s).")
        return "\n".join(lines)

    grades = data.get("grades")
    if isinstance(grades, list):
        if not grades:
            return "Nenhuma nota encontrada."
        return "\n".join(
            "- "
            + _line(
                [
                    short_course_name(item.get("name")),
                    f"{item['score']:.1f}%" if item.get("score") is not None else "sem nota",
                    f"{item.get('graded_count', 0)} corrigidas",
                    f"{item.get('missing_count', 0)} não entregues",
                ]
            )
            for item in grades[:20]
        )

    courses = data.get("courses")
    if isinstance(courses, list):
        if not courses:
            return "Nenhuma matéria encontrada."
        return "\n".join(
            "- "
            + _line(
                [
                    short_course_name(item.get("name")),
                    f"{item['score']:.1f}%" if item.get("score") is not None else "sem nota",
                    item.get("term"),
                ]
            )
            for item in courses[:20]
        )

    return None


def deterministic_fallback(observations: list[ToolObservation]) -> str | None:
    for obs in reversed(observations):
        data = obs.data
        if not isinstance(data, dict) or not data.get("ok"):
            continue
        body = _format_payload(data)
        if body:
            return "Não consegui confirmar a resposta, mas encontrei estes dados:\n" + body
    return None
