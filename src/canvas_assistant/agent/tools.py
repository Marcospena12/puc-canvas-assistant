from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from ..config import UserProfile
from ..htmltext import html_to_text
from ..llm.base import ToolSpec
from ..repository import Repository
from ..timeutil import (
    format_date_local,
    format_local,
    parse_dt,
    period_bounds,
    period_label,
    to_utc_iso,
    utc_now,
)

logger = logging.getLogger(__name__)


class ToolError(Exception):
    pass


class GetCoursesArgs(BaseModel):
    current_term_only: bool = Field(
        True,
        description="Se true, lista apenas cursos cujo período (term) cobre a data atual.",
    )
    include_non_subjects: bool = Field(
        False,
        description="Inclui espaços de coordenação, extensão e outros cursos que não são matérias.",
    )


class GetUpcomingArgs(BaseModel):
    period: Literal["today", "tomorrow", "this_week", "next_7_days", "next_30_days"] = Field(
        "next_7_days",
        description=(
            "Janela de datas para prazos: hoje, amanhã, esta semana, próximos 7 ou 30 dias."
        ),
    )
    course_id: int | None = Field(
        None,
        description="ID do curso no Canvas. Omita para considerar todas as matérias atuais.",
    )


class GetGradesArgs(BaseModel):
    course_id: int | None = Field(
        None,
        description="ID do curso no Canvas. Omita para considerar todas as matérias atuais.",
    )


class GetAssignmentsArgs(BaseModel):
    course_id: int = Field(..., description="ID do curso no Canvas (obtido com get_courses).")
    status: Literal["pending", "graded", "all"] = Field(
        "pending",
        description="pending = não entregues; graded = corrigidas; all = todas.",
    )
    limit: int = Field(20, ge=1, le=50, description="Máximo de tarefas retornadas.")


class GetAnnouncementsArgs(BaseModel):
    course_id: int | None = Field(
        None, description="ID do curso no Canvas. Omita para todos os cursos atuais."
    )
    days: int = Field(14, ge=1, le=90, description="Quantos dias para trás buscar.")
    limit: int = Field(10, ge=1, le=30, description="Máximo de avisos retornados.")


class ListFilesArgs(BaseModel):
    course_id: int | None = Field(
        None, description="ID do curso no Canvas. Omita para todas as matérias atuais."
    )
    query: str | None = Field(
        None, description="Texto para filtrar pelo nome do arquivo (opcional)."
    )
    limit: int = Field(30, ge=1, le=50, description="Máximo de arquivos retornados.")


ARGS_MODELS: dict[str, type[BaseModel]] = {
    "get_courses": GetCoursesArgs,
    "get_upcoming_items": GetUpcomingArgs,
    "get_grades": GetGradesArgs,
    "get_assignments": GetAssignmentsArgs,
    "get_announcements": GetAnnouncementsArgs,
    "list_course_files": ListFilesArgs,
}

TOOL_SPECS = [
    ToolSpec(
        name="get_courses",
        description=(
            "Lista as matérias/cursos ativos com id, nome, período, nota atual e professores. "
            "Use para 'quais matérias estou cursando', 'minhas notas' e para descobrir course_id."
        ),
        input_schema={},
    ),
    ToolSpec(
        name="get_upcoming_items",
        description=(
            "Lista tarefas, trabalhos e provas com prazo na janela pedida, com status de entrega. "
            "Use para 'o que tenho hoje', 'amanhã', 'essa semana'. Prefira period em vez de datas."
        ),
        input_schema={},
    ),
    ToolSpec(
        name="get_grades",
        description=(
            "Resumo de notas por matéria: nota atual (%), tarefas corrigidas, "
            "não entregues e atrasadas."
        ),
        input_schema={},
    ),
    ToolSpec(
        name="get_assignments",
        description=(
            "Lista tarefas de uma matéria específica (course_id), pendentes, corrigidas ou todas, "
            "com prazo e status."
        ),
        input_schema={},
    ),
    ToolSpec(
        name="get_announcements",
        description=(
            "Avisos recentes de professores e coordenação, com título, autor, data e um trecho. "
            "Use para 'novidades', 'avisos novos', 'o que o professor postou'."
        ),
        input_schema={},
    ),
    ToolSpec(
        name="list_course_files",
        description=(
            "Lista arquivos publicados (PDF, PPTX, DOCX etc.) com nome, tipo, "
            "tamanho, data e link. "
            "Pode filtrar por matéria e por texto no nome."
        ),
        input_schema={},
    ),
]


def _schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    _strip_titles(schema)
    return schema


def _strip_titles(node: Any) -> None:
    if isinstance(node, dict):
        node.pop("title", None)
        for value in node.values():
            _strip_titles(value)
    elif isinstance(node, list):
        for item in node:
            _strip_titles(item)


for _spec in TOOL_SPECS:
    _spec.input_schema = _schema(ARGS_MODELS[_spec.name])


def _tabs_state(tabs_json: str | None, tab_id: str) -> bool | None:
    if not tabs_json:
        return None
    try:
        tabs = json.loads(tabs_json)
    except json.JSONDecodeError:
        return None
    if not tabs:
        return None
    for tab in tabs:
        if str(tab.get("id") or "").casefold() == tab_id:
            return not bool(tab.get("hidden"))
    return False


def _human_size(size: int | None) -> str | None:
    if not size:
        return None
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return None


def _assignment_status(row: dict[str, Any]) -> str:
    if row.get("excused"):
        return "dispensada"
    if row.get("submitted_at"):
        if row.get("graded_at") or row.get("submission_state") == "graded":
            return "corrigida"
        return "entregue"
    if row.get("missing"):
        return "não entregue"
    due = parse_dt(row.get("due_at"))
    if row.get("late") or (due and due < utc_now()):
        return "atrasada"
    return "pendente"


class ToolRegistry:
    def __init__(self, repo: Repository, user: UserProfile, tz: ZoneInfo) -> None:
        self.repo = repo
        self.user = user
        self.tz = tz
        self._handlers: dict[str, Callable[[BaseModel], dict[str, Any]]] = {
            "get_courses": self._get_courses,
            "get_upcoming_items": self._get_upcoming,
            "get_grades": self._get_grades,
            "get_assignments": self._get_assignments,
            "get_announcements": self._get_announcements,
            "list_course_files": self._list_course_files,
        }

    @property
    def specs(self) -> list[ToolSpec]:
        return TOOL_SPECS

    def _default_course_ids(self, include_non_subjects: bool = False) -> list[int]:
        courses = self.repo.get_courses(
            self.user.user_id,
            current_only=True,
            include_non_subjects=include_non_subjects,
        )
        if not courses:
            courses = self.repo.get_courses(
                self.user.user_id,
                current_only=False,
                include_non_subjects=include_non_subjects,
            )
        return [int(course["id"]) for course in courses]

    def _require_course(self, course_id: int) -> dict[str, Any]:
        course = self.repo.get_course(self.user.user_id, course_id)
        if not course:
            raise ToolError(f"curso {course_id} não encontrado; use get_courses para ver os ids")
        return course

    def _get_courses(self, args: BaseModel) -> dict[str, Any]:
        parsed = GetCoursesArgs.model_validate(args)
        courses = self.repo.get_courses(
            self.user.user_id,
            current_only=parsed.current_term_only,
            include_non_subjects=parsed.include_non_subjects,
        )
        now_iso = utc_now().isoformat()
        data = []
        for course in courses:
            tabs_json = course.get("tabs_json")
            teachers = json.loads(course.get("teachers_json") or "[]")
            current = bool(
                course.get("term_start_at")
                and course.get("term_end_at")
                and course["term_start_at"] <= now_iso <= course["term_end_at"]
            )
            data.append(
                {
                    "course_id": course["id"],
                    "name": course["name"],
                    "code": course["course_code"],
                    "term": course.get("term_name"),
                    "current_term": current,
                    "is_subject": bool(course.get("is_subject")),
                    "score": round(course["current_score"], 2)
                    if course.get("current_score") is not None
                    else None,
                    "grade": course.get("current_grade"),
                    "teachers": teachers[:2],
                    "files_available": _tabs_state(tabs_json, "files"),
                    "announcements_available": _tabs_state(tabs_json, "announcements"),
                }
            )
        note = None
        if not data:
            note = "Nenhuma matéria encontrada para esse filtro."
        return {"courses": data, "count": len(data), "note": note}

    def _get_upcoming(self, args: BaseModel) -> dict[str, Any]:
        parsed = GetUpcomingArgs.model_validate(args)
        if parsed.course_id is not None:
            self._require_course(parsed.course_id)
            course_ids = [parsed.course_id]
        else:
            course_ids = self._default_course_ids()

        start, end = period_bounds(parsed.period, self.tz)
        rows = self.repo.get_upcoming_assignments(
            self.user.user_id,
            to_utc_iso(start),
            to_utc_iso(end),
            course_ids,
        )
        items = [self._assignment_payload(row) for row in rows[:40]]
        note = None
        if not items:
            note = f"Nenhum prazo em {period_label(parsed.period)}."
        return {
            "period": parsed.period,
            "window": (
                f"{format_date_local(start, self.tz)} a "
                f"{format_date_local(end - timedelta(seconds=1), self.tz)}"
            ),
            "items": items,
            "count": len(items),
            "note": note,
        }

    def _get_grades(self, args: BaseModel) -> dict[str, Any]:
        parsed = GetGradesArgs.model_validate(args)
        if parsed.course_id is not None:
            self._require_course(parsed.course_id)
            course_ids = [parsed.course_id]
        else:
            course_ids = self._default_course_ids()
        rows = self.repo.get_grades(self.user.user_id, course_ids)
        data = []
        for row in rows:
            data.append(
                {
                    "course_id": row["id"],
                    "name": row["name"],
                    "term": row.get("term_name"),
                    "score": round(row["current_score"], 2)
                    if row.get("current_score") is not None
                    else None,
                    "assignments_total": row["assignments_total"],
                    "graded_count": row["graded_count"],
                    "missing_count": row["missing_count"],
                    "overdue_count": row["overdue_count"],
                }
            )
        return {"grades": data, "count": len(data)}

    def _get_assignments(self, args: BaseModel) -> dict[str, Any]:
        parsed = GetAssignmentsArgs.model_validate(args)
        course = self._require_course(parsed.course_id)
        rows = self.repo.get_assignments(
            self.user.user_id,
            parsed.course_id,
            status=parsed.status,
            limit=parsed.limit,
        )
        items = [self._assignment_payload(row) for row in rows]
        note = None
        if not items:
            note = {
                "pending": "Nenhuma tarefa pendente nesta matéria.",
                "graded": "Nenhuma tarefa corrigida nesta matéria.",
                "all": "Nenhuma tarefa encontrada nesta matéria.",
            }[parsed.status]
        return {
            "course_id": parsed.course_id,
            "course": course["name"],
            "status": parsed.status,
            "assignments": items,
            "count": len(items),
            "note": note,
        }

    def _get_announcements(self, args: BaseModel) -> dict[str, Any]:
        parsed = GetAnnouncementsArgs.model_validate(args)
        if parsed.course_id is not None:
            self._require_course(parsed.course_id)
            course_ids = [parsed.course_id]
        else:
            course_ids = self._default_course_ids(include_non_subjects=True)

        now = utc_now()
        since = (now - timedelta(days=parsed.days)).isoformat()
        rows = self.repo.get_announcements(
            self.user.user_id,
            since_iso=since,
            course_ids=course_ids,
            limit=parsed.limit,
        )
        items = []
        for row in rows:
            items.append(
                {
                    "id": row["id"],
                    "course": row["course_name"],
                    "title": row["title"],
                    "author": row.get("author"),
                    "posted_at": format_local(row.get("posted_at"), self.tz),
                    "snippet": html_to_text(row.get("message_html"), limit=350),
                    "url": row.get("url"),
                }
            )
        note = None
        if not items:
            note = f"Nenhum aviso nos últimos {parsed.days} dias."
        return {"announcements": items, "count": len(items), "note": note}

    def _list_course_files(self, args: BaseModel) -> dict[str, Any]:
        parsed = ListFilesArgs.model_validate(args)
        if parsed.course_id is not None:
            course = self._require_course(parsed.course_id)
            course_ids = [parsed.course_id]
            selected_courses = [course]
        else:
            course_ids = self._default_course_ids()
            selected_courses = self.repo.get_courses(
                self.user.user_id, current_only=True, include_non_subjects=False
            )
            if not selected_courses:
                selected_courses = self.repo.get_courses(
                    self.user.user_id, current_only=False, include_non_subjects=False
                )

        files = self.repo.get_files(
            self.user.user_id,
            course_ids=course_ids,
            query=parsed.query,
            limit=max(parsed.limit * 3, 30),
        )
        module_items = self.repo.get_module_file_items(
            self.user.user_id,
            course_ids=course_ids,
            limit=120,
        )

        seen_ids: set[int] = set()
        seen_names: set[tuple[int, str]] = set()
        results: list[dict[str, Any]] = []

        for row in files:
            seen_ids.add(int(row["id"]))
            seen_names.add((int(row["course_id"]), str(row["display_name"]).casefold().strip()))
            results.append(
                {
                    "name": row["display_name"],
                    "course": row["course_name"],
                    "type": row.get("mime_class") or row.get("content_type"),
                    "size": _human_size(row.get("size")),
                    "updated_at": format_local(row.get("updated_at"), self.tz),
                    "url": f"{self.user.canvas_base_url}/courses/{row['course_id']}"
                    f"/files/{row['id']}",
                    "source": "files",
                }
            )

        query_folded = (parsed.query or "").casefold().strip()
        for item in module_items:
            title = str(item.get("title") or "")
            course_id = int(item["course_id"])
            name_key = (course_id, title.casefold().strip())
            content_id = item.get("content_id")
            if content_id and int(content_id) in seen_ids:
                continue
            if name_key in seen_names:
                continue
            if query_folded and query_folded not in title.casefold():
                continue
            seen_names.add(name_key)
            url = item.get("html_url") or item.get("url")
            if content_id:
                url = f"{self.user.canvas_base_url}/courses/{course_id}/files/{content_id}"
            results.append(
                {
                    "name": title,
                    "course": item["course_name"],
                    "type": "arquivo (módulo)",
                    "size": None,
                    "updated_at": None,
                    "url": url,
                    "source": "modules",
                }
            )

        notes = []
        if parsed.course_id is not None and not results:
            files_state = _tabs_state(selected_courses[0].get("tabs_json"), "files")
            if files_state is False:
                notes.append(
                    "A aba Arquivos não está disponível nesta matéria; "
                    "também não encontrei arquivos nos módulos."
                )
        if parsed.query and not results:
            notes.append(f"Nenhum arquivo encontrado para '{parsed.query}'.")

        return {
            "files": results[: parsed.limit],
            "count": min(len(results), parsed.limit),
            "notes": notes,
        }

    def _assignment_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        score = row.get("submission_score")
        status = _assignment_status(row)
        return {
            "id": row["id"],
            "course": row.get("course_name"),
            "title": row["name"],
            "due_at": format_local(row.get("due_at"), self.tz)
            if row.get("due_at")
            else "sem prazo",
            "type": "prova/quiz" if row.get("is_quiz") else "tarefa",
            "points": row.get("points_possible"),
            "status": status,
            "score": round(score, 2) if score is not None else None,
            "url": row.get("html_url"),
        }

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        handler = self._handlers.get(name)
        model = ARGS_MODELS.get(name)
        if not handler or not model:
            return json.dumps({"ok": False, "error": f"ferramenta desconhecida: {name}"})
        try:
            parsed = model.model_validate(arguments)
        except ValidationError as exc:
            details = exc.errors(include_url=False)
            return json.dumps(
                {"ok": False, "error": "argumentos inválidos", "details": details},
                ensure_ascii=False,
            )
        try:
            data = handler(parsed)
        except ToolError as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)
        except Exception:
            logger.exception("Erro interno na ferramenta %s", name)
            return json.dumps(
                {"ok": False, "error": "erro interno ao consultar os dados"},
                ensure_ascii=False,
            )
        return json.dumps({"ok": True, **data}, ensure_ascii=False)
