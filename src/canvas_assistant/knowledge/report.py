from __future__ import annotations

import re
import time
import unicodedata
from typing import Any
from zoneinfo import ZoneInfo

from ..config import UserProfile
from ..repository import Repository
from ..textutil import short_course_name
from ..timeutil import format_date_local, format_local, parse_dt
from .file_sync import FileSyncResult
from .search import SearchResultItem, SearchResultSet, search


def fold_text(value: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    without_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(without_accents.casefold().split())


def resolve_course(
    courses: list[dict[str, Any]],
    query: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    clean_query = (query or "").strip()
    if not clean_query:
        return None, []

    if clean_query.isdigit():
        target_id = int(clean_query)
        for course in courses:
            if int(course["id"]) == target_id:
                return course, []

    folded_query = fold_text(clean_query)
    if not folded_query:
        return None, []

    exact_matches: list[dict[str, Any]] = []
    partial_matches: list[dict[str, Any]] = []

    for course in courses:
        full_folded = fold_text(course.get("name"))
        short_name = short_course_name(course.get("name"))
        short_folded = fold_text(short_name)
        code_folded = fold_text(course.get("course_code"))

        # Acrônimo das palavras principais (ex.: Business Intelligence -> bi, DAM, TIPI)
        words = [
            w
            for w in re.split(r"[^\w]+", short_name)
            if w and fold_text(w) not in {"de", "em", "e", "da", "do", "para", "com", "no"}
        ]
        acronym = "".join(w[0] for w in words).lower() if words else ""

        if folded_query in (full_folded, short_folded, code_folded, acronym):
            exact_matches.append(course)
            continue

        if (
            folded_query in full_folded
            or folded_query in short_folded
            or folded_query in code_folded
            or full_folded in folded_query
            or short_folded in folded_query
        ):
            partial_matches.append(course)

    if len(exact_matches) == 1:
        return exact_matches[0], []
    if len(exact_matches) > 1:
        return None, exact_matches

    if len(partial_matches) == 1:
        return partial_matches[0], []
    if len(partial_matches) > 1:
        return None, partial_matches

    return None, []


def _human_size(size: int | None) -> str:
    if size is None or size <= 0:
        return "tamanho desconhecido"
    val = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if val < 1024 or unit == "GB":
            return f"{val:.0f} {unit}" if unit == "B" else f"{val:.1f} {unit}"
        val /= 1024
    return f"{size} B"


def _status_badge(row: dict[str, Any]) -> str:
    status = row.get("status")
    if status == "downloaded":
        return "baixado"
    if status == "pending":
        return "pendente"
    if status == "ignored":
        reason = row.get("skip_reason") or "ignorado"
        if reason.startswith("tipo_nao_suportado"):
            parts = reason.split(":")
            ext = parts[1] if len(parts) > 1 else "tipo"
            return f"ignorado: {ext}"
        if reason == "muito_grande":
            return "ignorado: >50MB"
        if reason in ("hidden", "locked"):
            return f"ignorado: {reason}"
        return f"ignorado: {reason}"
    if status == "failed":
        return "falha"
    return str(status or "desconhecido")


def build_files_text(
    repo: Repository,
    user: UserProfile,
    tz: ZoneInfo,
    query: str,
    *,
    limit: int = 40,
) -> str:
    courses = repo.get_courses(user.user_id, current_only=True, include_non_subjects=False)
    if not courses:
        courses = repo.get_courses(user.user_id, current_only=False, include_non_subjects=False)

    if not courses:
        return "Nenhuma matéria ativa encontrada no cache. Rode /sincronizar primeiro."

    clean_query = (query or "").strip()

    if not clean_query:
        lines = [
            "Arquivos por matéria ativa:",
        ]
        for course in courses:
            name = short_course_name(course.get("name"))
            rows = repo.get_file_sync_state(user.user_id, course_id=course["id"], active_only=True)
            downloaded = sum(1 for r in rows if r.get("status") == "downloaded")
            lines.append(f"- {name} ({downloaded}/{len(rows)} baixados)")
        lines.append("")
        lines.append("Use /arquivos <matéria> para ver a lista de uma matéria.")
        return "\n".join(lines)

    matched, candidates = resolve_course(courses, clean_query)

    if candidates:
        candidate_names = [short_course_name(c.get("name")) for c in candidates]
        return (
            f"Mais de uma matéria corresponde a '{clean_query}':\n"
            + "\n".join(f"- {n}" for n in candidate_names)
            + "\nEspecifique melhor o nome."
        )

    if not matched:
        available = [f"- {short_course_name(c.get('name'))}" for c in courses[:6]]
        return f"Não encontrei a matéria '{clean_query}'.\nMatérias disponíveis:\n" + "\n".join(
            available
        )

    course_id = int(matched["id"])
    course_name = short_course_name(matched.get("name"))
    rows = repo.get_file_sync_state(user.user_id, course_id=course_id, active_only=True)

    if not rows:
        course_sync = repo.get_file_course_sync(user.user_id, course_id) or {}
        if course_sync.get("files_tab_status") in ("forbidden", "unauthorized"):
            return (
                f"Nenhum arquivo disponível para {course_name}.\n"
                "A aba Arquivos desta matéria está bloqueada e não há arquivos nos módulos."
            )
        return f"Nenhum arquivo conhecido para {course_name}."

    total = len(rows)
    displayed = rows[:limit]
    lines = [f"Arquivos de {course_name} ({total}):"]

    for row in displayed:
        name = row.get("display_name") or f"arquivo_{row.get('file_id')}"
        size = _human_size(row.get("size"))
        badge = _status_badge(row)
        modified = format_date_local(row.get("modified_at"), tz)
        origin_parts = []
        if row.get("module_name"):
            origin_parts.append(f"módulo: {row['module_name']}")
        elif row.get("folder_name"):
            origin_parts.append(f"pasta: {row['folder_name']}")
        origin_str = f" | {', '.join(origin_parts)}" if origin_parts else ""

        lines.append(f"- {name} ({size}, {modified}) [{badge}]{origin_str}")

    if total > limit:
        lines.append(f"… e mais {total - limit} arquivos.")

    return "\n".join(lines)


def format_file_sync_result(result: FileSyncResult) -> str:
    lines = ["Sincronização de arquivos:"]
    lines.extend(f"- {line}" for line in result.as_lines())
    return "\n".join(lines)


# Cache em memória da última busca por usuário (user_id -> (timestamp, list[SearchResultItem]))
_LAST_SEARCH_CACHE: dict[str, tuple[float, list[SearchResultItem]]] = {}
_CACHE_TTL_SECONDS = 900.0  # 15 minutos


def perform_bot_search(
    repo: Repository,
    user_id: str,
    raw_input: str,
    *,
    tz: ZoneInfo | None = None,
) -> str:
    raw_clean = (raw_input or "").strip()
    if not raw_clean:
        return (
            "Uso: /buscar <termo>, /buscar <matéria>: <termo> "
            "ou /buscar <tipo>: <termo> (arquivo, pagina, tarefa, ementa, aviso)"
        )

    target_course_id: int | None = None
    target_source_types: list[str] | None = None
    query_text = raw_clean

    type_prefixes = {"arquivo", "pagina", "tarefa", "ementa", "aviso"}
    if ":" in query_text:
        head, rest = query_text.split(":", 1)
        if head.strip().casefold() in type_prefixes:
            target_source_types = [head.strip().casefold()]
            query_text = rest.strip()

    if ":" in query_text:
        course_part, query_part = query_text.split(":", 1)
        course_part = course_part.strip()
        query_part = query_part.strip()

        courses = repo.get_courses(user_id, current_only=True, include_non_subjects=False)
        if not courses:
            courses = repo.get_courses(user_id, current_only=False, include_non_subjects=False)

        matched, candidates = resolve_course(courses, course_part)
        if candidates:
            cand_names = [short_course_name(c.get("name")) for c in candidates]
            return f"Mais de uma matéria corresponde a '{course_part}':\n" + "\n".join(
                f"- {n}" for n in cand_names
            )
        if not matched:
            return (
                f"Matéria '{course_part}' não encontrada. Tente buscar sem especificar a matéria."
            )

        target_course_id = int(matched["id"])
        query_text = query_part

    if not query_text:
        return "Especifique o termo de busca. Exemplo: /buscar BI: modelagem"

    results = search(
        repo,
        user_id,
        query_text,
        course_id=target_course_id,
        source_types=target_source_types,
        k=5,
    )
    _LAST_SEARCH_CACHE[user_id] = (time.time(), results)

    return format_search_results(results, query_text, tz=tz)


def format_search_results(
    results: SearchResultSet | list[SearchResultItem],
    query: str,
    *,
    tz: ZoneInfo | None = None,
) -> str:
    if not results:
        return f"Nenhum resultado encontrado para '{query}'."

    lines: list[str] = []
    coverage = getattr(results, "coverage", "strong")
    if coverage == "weak":
        lines.append(
            "não encontrei o assunto completo no material; estes são os trechos mais próximos\n"
        )

    lines.append(f"Resultados para '{query}' ({len(results)}):")
    for idx, item in enumerate(results, start=1):
        courses_str = (
            ", ".join(short_course_name(c) for c in item.associated_courses)
            if item.associated_courses
            else short_course_name(item.course_name)
        )
        partial_info = ""
        if getattr(item, "match_mode", "all_terms") == "partial":
            partial_info = f" (parcial: {item.matched_terms} de {item.total_terms} termos)"

        type_tags = {
            "arquivo": "[Arquivo]",
            "pagina": "[Página]",
            "tarefa": "[Tarefa]",
            "aviso": "[Aviso]",
            "ementa": "[Ementa]",
        }
        stype = getattr(item, "source_type", "arquivo")
        tag = type_tags.get(stype, "[Arquivo]")
        due_display = None
        if getattr(item, "due_at", None):
            due_display = (
                format_local(item.due_at, tz) if tz and parse_dt(item.due_at) else item.due_at
            )
        due_str = f" | Prazo: {due_display}" if due_display else ""

        lines.append(f"\n<b>{idx}. {tag} {item.filename}</b> ({courses_str}){partial_info}")
        lines.append(f"<i>Local: {item.location_label}{due_str}</i>")
        lines.append(f"{item.snippet}")

    lines.append("\nUse /trecho <n> para ver o texto completo de um resultado.")
    return "\n".join(lines)


def get_user_snippet(user_id: str, arg: str) -> str:
    clean_arg = (arg or "").strip()
    if not clean_arg or not clean_arg.isdigit():
        return "Uso: /trecho <número do resultado da última busca>"

    target_idx = int(clean_arg)
    cached = _LAST_SEARCH_CACHE.get(user_id)
    if not cached:
        return "Nenhuma busca recente encontrada. Realize uma busca com /buscar primeiro."

    ts, items = cached
    if time.time() - ts > _CACHE_TTL_SECONDS:
        return "Os resultados da busca anterior expiraram. Faça a busca novamente."

    if target_idx < 1 or target_idx > len(items):
        return f"Número inválido. Escolha um índice entre 1 e {len(items)}."

    item = items[target_idx - 1]
    courses_str = (
        ", ".join(short_course_name(c) for c in item.associated_courses)
        if item.associated_courses
        else short_course_name(item.course_name)
    )

    header = (
        f"<b>Trecho {target_idx}: {item.filename}</b>\n"
        f"<i>Matéria: {courses_str} | {item.location_label}</i>\n"
    )
    return f"{header}\n{item.text}"
