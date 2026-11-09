from __future__ import annotations

from canvas_assistant.textutil import short_course_name


def test_short_course_name_removes_campus_suffix() -> None:
    name = (
        "Trabalho Interdisciplinar: Pesquisa em Informática - Sistemas de Informação "
        "- Campus Coração Eucarístico - PBE - Noite"
    )
    assert short_course_name(name) == "Trabalho Interdisciplinar: Pesquisa em Informática"


def test_short_course_name_keeps_subject_prefix() -> None:
    name = "Eixo 1 - Microfundamento: Fundamentos de Computação - Campus Lourdes"
    assert short_course_name(name) == "Eixo 1 - Microfundamento: Fundamentos de Computação"


def test_short_course_name_handles_empty() -> None:
    assert short_course_name(None) == ""
    assert short_course_name("  Cálculo   I  ") == "Cálculo I"
