from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ..canvas import CanvasAuthError, CanvasForbiddenError, CanvasService
from ..config import UserProfile
from ..repository import Repository
from ..textutil import short_course_name
from ..timeutil import format_local, parse_dt, to_local_iso, to_utc_iso
from .canvas_html import clean_canvas_html
from .chunking import build_chunks
from .extractors import DocumentUnit

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ContentSyncEvent:
    event_type: str  # "new_page" ou "updated_assignment"
    course_id: int
    course_name: str
    title: str
    ref: str


@dataclass(slots=True)
class ContentSyncResult:
    pages_ingested: int = 0
    assignments_ingested: int = 0
    announcements_ingested: int = 0
    syllabus_ingested: int = 0
    quizzes_ingested: int = 0
    chunks_created: int = 0
    events: list[ContentSyncEvent] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ContentSyncService:
    """Sincroniza e ingere páginas, tarefas, avisos, ementas e quizzes no FTS."""

    def __init__(
        self,
        repo: Repository,
        canvas: CanvasService,
        user: UserProfile,
        tz: ZoneInfo,
        *,
        blocked_retry_hours: int = 24,
    ) -> None:
        self.repo = repo
        self.canvas = canvas
        self.user = user
        self.tz = tz
        self.blocked_retry_hours = blocked_retry_hours

    def _is_blocked(self, course_id: int, resource: str) -> bool:
        state = self.repo.get_content_sync_state(self.user.user_id, course_id, resource)
        if not state or not state.get("retry_at"):
            return False
        retry_dt = parse_dt(str(state["retry_at"]))
        return bool(retry_dt and retry_dt > datetime.now(UTC))

    def _mark_success(self, course_id: int, resource: str) -> None:
        self.repo.set_content_sync_status(
            self.user.user_id, course_id, resource, "ok", success=True
        )

    def _handle_error(
        self, course_id: int, resource: str, exc: Exception, result: ContentSyncResult, label: str
    ) -> None:
        retry_at = None
        if isinstance(exc, (CanvasAuthError, CanvasForbiddenError)):
            retry_at = to_utc_iso(datetime.now(UTC) + timedelta(hours=self.blocked_retry_hours))
        self.repo.set_content_sync_status(
            self.user.user_id,
            course_id,
            resource,
            "blocked" if retry_at else "error",
            error=str(exc),
            retry_at=retry_at,
        )
        logger.warning("Falha ao sincronizar %s (%s): %s", label, course_id, exc)
        result.errors.append(f"{label}: {exc}")

    async def sync_all(
        self, *, notify_baseline: bool = False, force: bool = False
    ) -> ContentSyncResult:
        result = ContentSyncResult()
        courses = self.repo.get_courses(
            self.user.user_id, current_only=True, include_non_subjects=False
        )
        if not courses:
            courses = self.repo.get_courses(
                self.user.user_id, current_only=False, include_non_subjects=False
            )

        operations = (
            ("ementa", "Ementa", self._sync_course_syllabus),
            ("pagina", "Páginas", self._sync_course_pages),
            ("tarefa", "Tarefas", self._sync_course_assignments),
            ("aviso", "Avisos", self._sync_course_announcements),
            ("quiz", "Quizzes", self._sync_course_quizzes),
        )

        for course in courses:
            cid = int(course["id"])
            cname = short_course_name(course.get("name"))

            for resource, label, handler in operations:
                if self._is_blocked(cid, resource):
                    result.errors.append(f"{label} {cname}: bloqueada até nova tentativa")
                    continue
                try:
                    if resource in ("pagina", "tarefa"):
                        await handler(
                            cid,
                            cname,
                            result,
                            notify_baseline=notify_baseline,
                            force=force,
                        )
                    else:
                        await handler(cid, cname, result, force=force)
                    self._mark_success(cid, resource)
                except Exception as exc:
                    self._handle_error(cid, resource, exc, result, label)

        return result

    async def _sync_course_syllabus(
        self, course_id: int, course_name: str, result: ContentSyncResult, *, force: bool = False
    ) -> None:
        try:
            details = await self.canvas.client.get(
                f"courses/{course_id}", params={"include[]": ["syllabus_body"]}
            )
        except (CanvasAuthError, CanvasForbiddenError):
            raise
        except Exception:
            return

        body = (details.get("syllabus_body") or "").strip()
        if not body:
            return

        html_res = clean_canvas_html(body)
        text = html_res.text.strip()
        if len(text) < 20:
            return

        body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = self.repo.get_canvas_content_state(
            self.user.user_id, course_id, "ementa", str(course_id)
        )

        if (
            not force
            and existing
            and existing.get("content_hash") == body_hash
            and existing.get("active")
        ):
            return

        units = [
            DocumentUnit(
                location_type="secao", index_start=1, index_end=1, label="Ementa", text=text
            )
        ]
        chunks = build_chunks(units)
        chunks_payload = [
            {
                "chunk_index": c.chunk_index,
                "location_type": c.location_type,
                "location_label": c.location_label,
                "location_start": c.location_start,
                "location_end": c.location_end,
                "text": c.text,
                "text_hash": c.text_hash,
                "char_count": c.char_count,
                "token_estimate": c.token_estimate,
            }
            for c in chunks
        ]

        self.repo.replace_content_chunks(
            self.user.user_id,
            course_id,
            "ementa",
            str(course_id),
            f"Ementa - {course_name}",
            chunks_payload,
        )
        self.repo.upsert_canvas_content_state(
            self.user.user_id,
            {
                "course_id": course_id,
                "source_type": "ementa",
                "source_ref": str(course_id),
                "title": f"Ementa - {course_name}",
                "content_hash": body_hash,
                "canvas_file_ids_json": json.dumps(list(html_res.canvas_file_ids)),
                "active": 1,
            },
        )
        result.syllabus_ingested += 1
        result.chunks_created += len(chunks)

    async def _sync_course_pages(
        self,
        course_id: int,
        course_name: str,
        result: ContentSyncResult,
        *,
        notify_baseline: bool,
        force: bool = False,
    ) -> None:
        page_items: list[dict[str, Any]] = []
        try:
            page_items = await self.canvas.client.get_all(
                f"courses/{course_id}/pages", params={"per_page": 100}
            )
        except (CanvasAuthError, CanvasForbiddenError):
            raise
        except Exception:
            # Fallback: obtém páginas a partir dos módulos
            try:
                modules = await self.canvas.get_modules(course_id)
                for m in modules:
                    for it in m.get("items", []):
                        if it.get("type") == "Page" and it.get("page_url"):
                            page_items.append(
                                {"url": it["page_url"], "title": it.get("title") or it["page_url"]}
                            )
            except (CanvasAuthError, CanvasForbiddenError):
                raise
            except Exception:
                return

        active_urls: set[str] = set()
        for p in page_items:
            page_url = p.get("url") or p.get("page_url")
            if not page_url:
                continue
            active_urls.add(page_url)

            try:
                full_page = await self.canvas.client.get(f"courses/{course_id}/pages/{page_url}")
            except (CanvasAuthError, CanvasForbiddenError):
                raise
            except Exception as exc:
                logger.debug("Falha ao ler página %s (%s): %s", page_url, course_id, exc)
                continue

            raw_body = full_page.get("body") or ""
            title = full_page.get("title") or p.get("title") or page_url
            html_res = clean_canvas_html(raw_body)
            text = html_res.text.strip()
            if not text:
                continue

            body_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            existing = self.repo.get_canvas_content_state(
                self.user.user_id, course_id, "pagina", page_url
            )

            if (
                not force
                and existing
                and existing.get("content_hash") == body_hash
                and existing.get("active")
            ):
                continue

            # Se for página nova e baseline já ativo
            if notify_baseline and not existing:
                result.events.append(
                    ContentSyncEvent(
                        event_type="new_page",
                        course_id=course_id,
                        course_name=course_name,
                        title=title,
                        ref=page_url,
                    )
                )

            units = [
                DocumentUnit(
                    location_type="pagina",
                    index_start=1,
                    index_end=1,
                    label=f"Página: {title}",
                    text=text,
                )
            ]
            chunks = build_chunks(units)
            chunks_payload = [
                {
                    "chunk_index": c.chunk_index,
                    "location_type": c.location_type,
                    "location_label": c.location_label,
                    "location_start": c.location_start,
                    "location_end": c.location_end,
                    "text": c.text,
                    "text_hash": c.text_hash,
                    "char_count": c.char_count,
                    "token_estimate": c.token_estimate,
                }
                for c in chunks
            ]

            self.repo.replace_content_chunks(
                self.user.user_id, course_id, "pagina", page_url, title, chunks_payload
            )
            self.repo.upsert_canvas_content_state(
                self.user.user_id,
                {
                    "course_id": course_id,
                    "source_type": "pagina",
                    "source_ref": page_url,
                    "title": title,
                    "content_hash": body_hash,
                    "canvas_file_ids_json": json.dumps(list(html_res.canvas_file_ids)),
                    "active": 1,
                },
            )
            result.pages_ingested += 1
            result.chunks_created += len(chunks)

        self.repo.mark_missing_content_inactive(self.user.user_id, course_id, "pagina", active_urls)

    async def _sync_course_assignments(
        self,
        course_id: int,
        course_name: str,
        result: ContentSyncResult,
        *,
        notify_baseline: bool,
        force: bool = False,
    ) -> None:
        try:
            assignments = await self.canvas.get_assignments(course_id)
        except (CanvasAuthError, CanvasForbiddenError):
            raise
        except Exception:
            return

        active_refs: set[str] = set()
        for a in assignments:
            aid = str(a.get("id"))
            if not aid:
                continue
            active_refs.add(aid)

            desc_raw = a.get("description") or ""
            if not desc_raw.strip():
                continue

            html_res = clean_canvas_html(desc_raw)
            desc_text = html_res.text.strip()
            if not desc_text:
                continue

            title = a.get("name") or f"Tarefa {aid}"
            due_dt = parse_dt(a.get("due_at"))
            due_str = format_local(a.get("due_at"), self.tz) if due_dt else "sem prazo"
            due_iso = to_local_iso(a.get("due_at"), self.tz)
            points = a.get("points_possible")
            points_str = f"{points:.1f} pts" if points is not None else "sem pontuação"
            types_list = a.get("submission_types") or []
            types_str = ", ".join(types_list) if types_list else "entrega padrão"

            header = (
                f"# Tarefa: {title}\n"
                f"Prazo: {due_str} | Pontos: {points_str} | Tipos de entrega: {types_str}\n\n"
            )
            full_text = f"{header}{desc_text}"

            # Inclui rubrica se presente
            rubric = a.get("rubric") or []
            if rubric:
                rubric_lines = ["\n\nCritérios de Avaliação (Rubrica):"]
                for crit in rubric:
                    c_desc = crit.get("description", "")
                    c_pts = crit.get("points", "")
                    rubric_lines.append(f"• {c_desc} ({c_pts} pts)")
                full_text = full_text + "\n".join(rubric_lines)

            body_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
            existing = self.repo.get_canvas_content_state(
                self.user.user_id, course_id, "tarefa", aid
            )

            if (
                not force
                and existing
                and existing.get("content_hash") == body_hash
                and existing.get("active")
            ):
                continue

            # Se for tarefa já conhecida mas cuja descrição mudou
            if notify_baseline and existing and existing.get("content_hash") != body_hash:
                result.events.append(
                    ContentSyncEvent(
                        event_type="updated_assignment",
                        course_id=course_id,
                        course_name=course_name,
                        title=title,
                        ref=aid,
                    )
                )

            units = [
                DocumentUnit(
                    location_type="tarefa",
                    index_start=1,
                    index_end=1,
                    label=f"Tarefa: {title}",
                    text=full_text,
                )
            ]
            chunks = build_chunks(units)
            chunks_payload = [
                {
                    "chunk_index": c.chunk_index,
                    "location_type": c.location_type,
                    "location_label": c.location_label,
                    "location_start": c.location_start,
                    "location_end": c.location_end,
                    "text": c.text,
                    "text_hash": c.text_hash,
                    "char_count": c.char_count,
                    "token_estimate": c.token_estimate,
                }
                for c in chunks
            ]

            self.repo.replace_content_chunks(
                self.user.user_id, course_id, "tarefa", aid, title, chunks_payload, due_at=due_iso
            )
            self.repo.upsert_canvas_content_state(
                self.user.user_id,
                {
                    "course_id": course_id,
                    "source_type": "tarefa",
                    "source_ref": aid,
                    "title": title,
                    "content_hash": body_hash,
                    "canvas_file_ids_json": json.dumps(list(html_res.canvas_file_ids)),
                    "due_at": due_iso,
                    "active": 1,
                },
            )
            result.assignments_ingested += 1
            result.chunks_created += len(chunks)

        self.repo.mark_missing_content_inactive(
            self.user.user_id, course_id, "tarefa", active_refs, not_prefix="quiz_"
        )

    async def _sync_course_announcements(
        self,
        course_id: int,
        course_name: str,
        result: ContentSyncResult,
        *,
        force: bool = False,
    ) -> None:
        now = datetime.now(UTC)
        start_iso = (now - timedelta(days=180)).isoformat()
        end_iso = now.isoformat()
        try:
            announcements = await self.canvas.get_announcements(course_id, start_iso, end_iso)
        except (CanvasAuthError, CanvasForbiddenError):
            raise
        except Exception:
            return

        active_refs: set[str] = set()
        for ann in announcements:
            ann_id = str(ann.get("id"))
            if not ann_id:
                continue
            active_refs.add(ann_id)

            raw_msg = ann.get("message") or ""
            if not raw_msg.strip():
                continue

            html_res = clean_canvas_html(raw_msg)
            msg_text = html_res.text.strip()
            if not msg_text:
                continue

            title = ann.get("title") or f"Aviso {ann_id}"
            author = ann.get("user_name") or "Professor"
            posted_dt = parse_dt(ann.get("posted_at"))
            posted_str = (
                format_local(ann.get("posted_at"), self.tz) if posted_dt else "data não informada"
            )
            posted_iso = to_local_iso(ann.get("posted_at"), self.tz)

            header = f"# Aviso: {title}\nAutor: {author} | Postado em: {posted_str}\n\n"
            full_text = f"{header}{msg_text}"

            body_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
            existing = self.repo.get_canvas_content_state(
                self.user.user_id, course_id, "aviso", ann_id
            )

            if (
                not force
                and existing
                and existing.get("content_hash") == body_hash
                and existing.get("active")
            ):
                continue

            units = [
                DocumentUnit(
                    location_type="aviso",
                    index_start=1,
                    index_end=1,
                    label=f"Aviso: {title}",
                    text=full_text,
                )
            ]
            chunks = build_chunks(units)
            chunks_payload = [
                {
                    "chunk_index": c.chunk_index,
                    "location_type": c.location_type,
                    "location_label": c.location_label,
                    "location_start": c.location_start,
                    "location_end": c.location_end,
                    "text": c.text,
                    "text_hash": c.text_hash,
                    "char_count": c.char_count,
                    "token_estimate": c.token_estimate,
                }
                for c in chunks
            ]

            self.repo.replace_content_chunks(
                self.user.user_id, course_id, "aviso", ann_id, title, chunks_payload
            )
            self.repo.upsert_canvas_content_state(
                self.user.user_id,
                {
                    "course_id": course_id,
                    "source_type": "aviso",
                    "source_ref": ann_id,
                    "title": title,
                    "content_hash": body_hash,
                    "canvas_file_ids_json": json.dumps(list(html_res.canvas_file_ids)),
                    "due_at": posted_iso,
                    "active": 1,
                },
            )
            result.announcements_ingested += 1
            result.chunks_created += len(chunks)

        self.repo.mark_missing_content_inactive(self.user.user_id, course_id, "aviso", active_refs)

    async def _sync_course_quizzes(
        self,
        course_id: int,
        course_name: str,
        result: ContentSyncResult,
        *,
        force: bool = False,
    ) -> None:
        try:
            quizzes = await self.canvas.client.get_all(f"courses/{course_id}/quizzes")
        except (CanvasAuthError, CanvasForbiddenError):
            raise
        except Exception:
            return

        active_refs: set[str] = set()
        for q in quizzes:
            qid = str(q.get("id"))
            if not qid:
                continue
            active_refs.add(qid)

            desc_raw = q.get("description") or ""
            title = q.get("title") or f"Quiz {qid}"
            due_dt = parse_dt(q.get("due_at"))
            due_str = format_local(q.get("due_at"), self.tz) if due_dt else "sem prazo"
            due_iso = to_local_iso(q.get("due_at"), self.tz)
            points = q.get("points_possible")
            points_str = f"{points:.1f} pts" if points is not None else "sem pontuação"

            html_res = clean_canvas_html(desc_raw)
            desc_text = html_res.text.strip()

            header = f"# Quiz/Prova: {title}\nPrazo: {due_str} | Pontos: {points_str}\n\n"
            full_text = f"{header}{desc_text}" if desc_text else header

            body_hash = hashlib.sha256(full_text.encode("utf-8")).hexdigest()
            existing = self.repo.get_canvas_content_state(
                self.user.user_id, course_id, "tarefa", f"quiz_{qid}"
            )

            if (
                not force
                and existing
                and existing.get("content_hash") == body_hash
                and existing.get("active")
            ):
                continue

            units = [
                DocumentUnit(
                    location_type="tarefa",
                    index_start=1,
                    index_end=1,
                    label=f"Quiz: {title}",
                    text=full_text,
                )
            ]
            chunks = build_chunks(units)
            chunks_payload = [
                {
                    "chunk_index": c.chunk_index,
                    "location_type": c.location_type,
                    "location_label": c.location_label,
                    "location_start": c.location_start,
                    "location_end": c.location_end,
                    "text": c.text,
                    "text_hash": c.text_hash,
                    "char_count": c.char_count,
                    "token_estimate": c.token_estimate,
                }
                for c in chunks
            ]

            self.repo.replace_content_chunks(
                self.user.user_id,
                course_id,
                "tarefa",
                f"quiz_{qid}",
                title,
                chunks_payload,
                due_at=due_iso,
            )
            self.repo.upsert_canvas_content_state(
                self.user.user_id,
                {
                    "course_id": course_id,
                    "source_type": "tarefa",
                    "source_ref": f"quiz_{qid}",
                    "title": title,
                    "content_hash": body_hash,
                    "canvas_file_ids_json": json.dumps(list(html_res.canvas_file_ids)),
                    "due_at": due_iso,
                    "active": 1,
                },
            )
            result.quizzes_ingested += 1
            result.chunks_created += len(chunks)

        self.repo.mark_missing_content_inactive(
            self.user.user_id, course_id, "tarefa", active_refs, prefix="quiz_"
        )
