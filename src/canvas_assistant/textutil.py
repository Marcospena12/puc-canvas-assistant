from __future__ import annotations

_SUFFIX_MARKERS = ("campus", "pbe", "noite", "sistemas de informação")


def short_course_name(name: str | None) -> str:
    """Remove campus/turno/período do nome, preservando o nome da matéria."""
    value = " ".join((name or "").split())
    if not value:
        return ""
    parts = value.split(" - ")
    for index, part in enumerate(parts[1:], start=1):
        if any(marker in part.casefold() for marker in _SUFFIX_MARKERS):
            return " - ".join(parts[:index]).strip()
    return value
