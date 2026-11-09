from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from zoneinfo import ZoneInfo

from ..canvas import CanvasError, CanvasService
from ..config import FileSyncConfig, UserProfile
from ..repository import Repository
from ..sync import parse_file, tab_enabled
from ..timeutil import parse_dt, to_utc_iso, utc_now
from .downloader import DownloadError, FileDownloader, FileTooLargeError
from .extraction import ExtractionPipeline

logger = logging.getLogger(__name__)

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "COM1",
        "COM2",
        "COM3",
        "COM4",
        "COM5",
        "COM6",
        "COM7",
        "COM8",
        "COM9",
        "LPT1",
        "LPT2",
        "LPT3",
        "LPT4",
        "LPT5",
        "LPT6",
        "LPT7",
        "LPT8",
        "LPT9",
    }
)

_EXT_BY_MIME = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "docx",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/x-markdown": "md",
}


def sanitize_filename(name: str | None, fallback: str = "arquivo") -> str:
    base = Path(name or "").name
    base = _INVALID_CHARS.sub("_", base)
    base = " ".join(base.split()).strip(" .")
    if not base:
        base = fallback
    stem, suffix = os.path.splitext(base)
    if stem.upper() in _RESERVED_NAMES:
        stem = f"_{stem}"
    max_stem = max(1, 120 - len(suffix))
    stem = stem[:max_stem].rstrip(" .")
    return f"{stem}{suffix}"


def detect_file_extension(filename: str | None, content_type: str | None) -> str | None:
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    if suffix:
        return suffix
    clean_mime = (content_type or "").split(";")[0].strip().lower()
    return _EXT_BY_MIME.get(clean_mime)


@dataclass
class FileSyncResult:
    courses: int = 0
    files_seen: int = 0
    new_files: int = 0
    downloaded: int = 0
    unchanged: int = 0
    ignored: int = 0
    failed: int = 0
    bytes_downloaded: int = 0
    blocked_courses: list[str] = field(default_factory=list)
    ignored_reasons: dict[str, int] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        lines = [
            f"matérias: {self.courses}",
            f"arquivos vistos: {self.files_seen}",
            f"novos: {self.new_files}",
            f"baixados: {self.downloaded}",
            f"inalterados: {self.unchanged}",
            f"ignorados: {self.ignored}",
            f"falhas: {self.failed}",
        ]
        if self.bytes_downloaded:
            mb = self.bytes_downloaded / (1024 * 1024)
            lines.append(f"volume baixado: {mb:.1f} MB")
        if self.ignored_reasons:
            reasons_summary = ", ".join(f"{k}={v}" for k, v in sorted(self.ignored_reasons.items()))
            lines.append(f"motivos de exclusão: {reasons_summary}")
        if self.blocked_courses:
            lines.append(f"matérias bloqueadas ({len(self.blocked_courses)}):")
            lines.extend(f"- {name}" for name in self.blocked_courses[:5])
        if self.issues:
            lines.append("avisos:")
            lines.extend(f"- {issue}" for issue in self.issues[:8])
        return lines


class FileSyncService:
    def __init__(
        self,
        repo: Repository,
        canvas: CanvasService,
        user: UserProfile,
        tz: ZoneInfo,
        config: FileSyncConfig,
        downloader: FileDownloader,
    ) -> None:
        self.repo = repo
        self.canvas = canvas
        self.user = user
        self.tz = tz
        self.config = config
        self.downloader = downloader

    def target_courses(self) -> list[dict[str, Any]]:
        user_id = self.user.user_id
        courses = self.repo.get_courses(user_id, current_only=True, include_non_subjects=False)
        if not courses:
            courses = self.repo.get_courses(user_id, current_only=False, include_non_subjects=False)
        return courses

    async def sync_all(self, *, download: bool = True) -> FileSyncResult:
        result = FileSyncResult()
        courses = self.target_courses()
        result.courses = len(courses)

        for course in courses:
            try:
                await self._sync_course(course, download=download, result=result)
            except Exception as exc:
                logger.exception("Falha na sincronização de arquivos do curso %s", course.get("id"))
                result.issues.append(f"curso {course.get('id')}: {exc}")

        if download and result.downloaded > 0:
            try:
                pipeline = ExtractionPipeline(self.repo, self.user, self.config.files_dir)
                ext_run = pipeline.run(force_all=False)
                logger.info(
                    "Extração automática pós-download: %s extraídos, %s chunks",
                    ext_run.extracted,
                    ext_run.total_chunks_created,
                )
            except Exception as exc:
                logger.exception("Falha na extração automática pós-download")
                result.issues.append(f"extração automática: {exc}")

        return result

    async def _fetch_folder_map(self, course_id: int) -> dict[int, str]:
        try:
            folders = await self.canvas.get_folders(course_id)
        except CanvasError as exc:
            logger.debug("Não foi possível listar pastas do curso %s: %s", course_id, exc)
            return {}
        folder_map: dict[int, str] = {}
        for item in folders:
            folder_id = item.get("id")
            if folder_id is not None:
                name = str(item.get("full_name") or item.get("name") or "").strip()
                if name:
                    folder_map[int(folder_id)] = name
        return folder_map

    async def _sync_course(
        self,
        course: dict[str, Any],
        *,
        download: bool,
        result: FileSyncResult,
    ) -> None:
        user_id = self.user.user_id
        course_id = int(course["id"])
        course_name = str(course.get("name") or f"Curso {course_id}")

        course_sync = self.repo.get_file_course_sync(user_id, course_id) or {}
        files_retry_str = course_sync.get("files_retry_at")
        files_blocked = False
        if files_retry_str:
            retry_dt = parse_dt(files_retry_str)
            if retry_dt and retry_dt > utc_now():
                files_blocked = True
                result.blocked_courses.append(course_name)

        tabs: list[dict[str, Any]] = []
        try:
            raw_tabs = course.get("tabs_json")
            if raw_tabs:
                import json

                tabs = json.loads(raw_tabs)
        except Exception:
            tabs = []
        files_tab_state = tab_enabled(tabs, "files")

        cached_files = self.repo.get_files(
            user_id,
            course_ids=[course_id],
            include_hidden=True,
            limit=10000,
        )
        cached_modules = self.repo.get_module_file_items(
            user_id,
            course_ids=[course_id],
            limit=10000,
        )

        records: dict[int, dict[str, Any]] = {}

        for row in cached_files:
            file_id = int(row["id"])
            records[file_id] = {
                "file_id": file_id,
                "course_id": course_id,
                "display_name": row["display_name"],
                "filename": row.get("filename") or row["display_name"],
                "content_type": row.get("content_type"),
                "size": row.get("size"),
                "modified_at": row.get("updated_at"),
                "folder_id": row.get("folder_id"),
                "folder_name": None,
                "locked": bool(row.get("locked")),
                "hidden": bool(row.get("hidden")),
                "module_id": None,
                "module_name": None,
                "source": "files",
                "download_url": row.get("url"),
            }

        for item in cached_modules:
            content_id = item.get("content_id")
            if not content_id:
                continue
            file_id = int(content_id)
            if file_id in records:
                existing = records[file_id]
                existing["source"] = "both"
                if existing.get("module_id") is None:
                    existing["module_id"] = item.get("module_id")
                    existing["module_name"] = item.get("module_name")
            else:
                title = str(item.get("title") or f"Arquivo {file_id}")
                detail: dict[str, Any] = {}
                try:
                    raw_detail = await self.canvas.get_file(course_id, file_id)
                    parsed = parse_file(raw_detail, course_id)
                    detail = {
                        "display_name": parsed.display_name,
                        "filename": parsed.filename,
                        "content_type": parsed.content_type,
                        "size": parsed.size,
                        "modified_at": parsed.updated_at,
                        "folder_id": parsed.folder_id,
                        "locked": parsed.locked,
                        "hidden": parsed.hidden,
                        "download_url": parsed.url,
                    }
                except CanvasError as exc:
                    logger.debug(
                        "Falha ao buscar metadados do arquivo de módulo %s: %s", file_id, exc
                    )
                    detail = {
                        "display_name": title,
                        "filename": title,
                        "content_type": None,
                        "size": None,
                        "modified_at": None,
                        "folder_id": None,
                        "locked": False,
                        "hidden": False,
                        "download_url": None,
                    }

                records[file_id] = {
                    "file_id": file_id,
                    "course_id": course_id,
                    "display_name": detail.get("display_name") or title,
                    "filename": detail.get("filename") or title,
                    "content_type": detail.get("content_type"),
                    "size": detail.get("size"),
                    "modified_at": detail.get("modified_at"),
                    "folder_id": detail.get("folder_id"),
                    "folder_name": None,
                    "locked": bool(detail.get("locked")),
                    "hidden": bool(detail.get("hidden")),
                    "module_id": item.get("module_id"),
                    "module_name": item.get("module_name"),
                    "source": "modules",
                    "download_url": detail.get("download_url"),
                }

        if files_tab_state is False:
            records = {fid: rec for fid, rec in records.items() if rec.get("source") != "files"}

        folder_map: dict[int, str] = {}
        has_folders = any(rec.get("folder_id") for rec in records.values())
        if has_folders and files_tab_state is not False and not files_blocked:
            folder_map = await self._fetch_folder_map(course_id)

        now_iso = to_utc_iso(utc_now())
        state_map = self.repo.get_file_sync_states(user_id, course_id)
        previously_active = {fid for fid, st in state_map.items() if st.get("active")}
        new_count = len(set(records.keys()) - previously_active)

        self.repo.mark_file_sync_inactive(user_id, course_id)

        upsert_rows: list[dict[str, Any]] = []
        for file_id, rec in records.items():
            existing = state_map.get(file_id)
            folder_id = rec.get("folder_id")
            resolved_folder = folder_map.get(folder_id) if folder_id else None
            if not resolved_folder and existing:
                resolved_folder = existing.get("folder_name")
            rec["folder_name"] = resolved_folder

            existing_mod = existing.get("modified_at") if existing else None
            incoming_mod = rec.get("modified_at")
            existing_size = existing.get("size") if existing else None
            incoming_size = rec.get("size")

            meta_changed = (
                existing is None
                or (
                    existing_mod is not None
                    and incoming_mod is not None
                    and existing_mod != incoming_mod
                )
                or (
                    existing_size is not None
                    and incoming_size is not None
                    and existing_size != incoming_size
                )
            )

            if existing and not meta_changed:
                status = existing.get("status") or "pending"
                skip_reason = existing.get("skip_reason")
                error = existing.get("error")
                next_retry = existing.get("next_retry_at")
                local_path = existing.get("local_path")
                sha256 = existing.get("sha256")
                downloaded_at = existing.get("downloaded_at")
            else:
                status = "pending"
                skip_reason = None
                error = None
                next_retry = None
                local_path = None
                sha256 = None
                downloaded_at = None

            row = {
                "file_id": file_id,
                "course_id": course_id,
                "display_name": rec["display_name"],
                "filename": rec.get("filename") or "",
                "content_type": rec.get("content_type"),
                "size": rec.get("size"),
                "modified_at": rec.get("modified_at"),
                "folder_id": rec.get("folder_id"),
                "folder_name": rec.get("folder_name"),
                "locked": rec.get("locked"),
                "hidden": rec.get("hidden"),
                "module_id": rec.get("module_id"),
                "module_name": rec.get("module_name"),
                "source": rec.get("source"),
                "download_url": rec.get("download_url"),
                "local_path": local_path,
                "sha256": sha256,
                "status": status,
                "skip_reason": skip_reason,
                "error": error,
                "attempts": existing.get("attempts", 0) if existing else 0,
                "last_attempt_at": existing.get("last_attempt_at") if existing else None,
                "next_retry_at": next_retry,
                "downloaded_at": downloaded_at,
                "first_seen_at": existing.get("first_seen_at") if existing else now_iso,
                "updated_at": now_iso,
                "active": 1,
            }
            upsert_rows.append(row)

        self.repo.upsert_file_sync_states(user_id, upsert_rows)

        course_downloaded = course_unchanged = course_ignored = course_failed = 0

        if download:
            current_states = self.repo.get_file_sync_states(user_id, course_id)
            for file_id, rec in records.items():
                current_state = current_states.get(file_id) or {}
                outcome, outcome_reason = await self._process_file(
                    course_id, rec, current_state, files_blocked
                )
                if outcome == "downloaded":
                    course_downloaded += 1
                elif outcome == "unchanged":
                    course_unchanged += 1
                elif outcome == "ignored":
                    course_ignored += 1
                    if outcome_reason:
                        result.ignored_reasons[outcome_reason] = (
                            result.ignored_reasons.get(outcome_reason, 0) + 1
                        )
                elif outcome == "failed":
                    course_failed += 1

        result.files_seen += len(records)
        result.new_files += new_count
        result.downloaded += course_downloaded
        result.unchanged += course_unchanged
        result.ignored += course_ignored
        result.failed += course_failed

        self.repo.upsert_file_course_stats(
            user_id,
            course_id,
            files_seen=len(records),
            new_files=new_count,
            downloaded=course_downloaded,
            unchanged=course_unchanged,
            ignored=course_ignored,
            failed=course_failed,
        )

    async def _process_file(
        self,
        course_id: int,
        record: dict[str, Any],
        state: dict[str, Any],
        files_blocked: bool,
    ) -> tuple[str, str | None]:
        user_id = self.user.user_id
        file_id = int(record["file_id"])
        display_name = record.get("display_name") or f"file_{file_id}"
        now_dt = utc_now()
        now_iso = to_utc_iso(now_dt)

        if record.get("hidden"):
            self.repo.update_file_sync_state(
                user_id, file_id, status="ignored", skip_reason="hidden", error=None
            )
            return "ignored", "hidden"

        if record.get("locked"):
            self.repo.update_file_sync_state(
                user_id, file_id, status="ignored", skip_reason="locked", error=None
            )
            return "ignored", "locked"

        extension = detect_file_extension(
            record.get("filename") or display_name,
            record.get("content_type"),
        )
        if not extension or extension.lower() not in self.config.allowed_types:
            reason = f"tipo_nao_suportado:{extension or 'desconhecido'}"
            self.repo.update_file_sync_state(
                user_id, file_id, status="ignored", skip_reason=reason, error=None
            )
            return "ignored", reason

        size = record.get("size")
        if size is not None and size > self.config.max_size_bytes:
            reason = "muito_grande"
            self.repo.update_file_sync_state(
                user_id, file_id, status="ignored", skip_reason=reason, error=None
            )
            return "ignored", reason

        download_url = record.get("download_url") or state.get("download_url")
        if not download_url:
            retry_at = to_utc_iso(now_dt + timedelta(hours=self.config.download_retry_hours))
            self.repo.update_file_sync_state(
                user_id,
                file_id,
                status="failed",
                error="URL de download ausente",
                next_retry_at=retry_at,
            )
            return "failed", "sem_url"

        if files_blocked and record.get("source") == "files":
            return "unchanged", None

        previous_status = state.get("status")
        previous_path = state.get("local_path")
        previous_hash = state.get("sha256")
        next_retry_str = state.get("next_retry_at")

        if previous_status == "failed" and next_retry_str:
            retry_dt = parse_dt(next_retry_str)
            if retry_dt and retry_dt > now_dt:
                return "failed", state.get("error")

        if previous_status == "downloaded" and previous_path and previous_hash:
            dest = self.config.files_dir / Path(previous_path)
            if dest.is_file():
                return "unchanged", None

        sanitized = sanitize_filename(display_name, f"arquivo_{file_id}")
        if not Path(sanitized).suffix and extension:
            sanitized = f"{sanitized}.{extension}"
        rel_path = PurePosixPath(str(course_id)) / f"{file_id}_{sanitized}"
        target_path = self.config.files_dir / Path(rel_path)

        attempts = int(state.get("attempts") or 0) + 1
        try:
            download_result = await self.downloader.download(
                download_url,
                target_path,
                max_bytes=self.config.max_size_bytes,
                expected_size=size,
            )
            self.repo.update_file_sync_state(
                user_id,
                file_id,
                status="downloaded",
                local_path=rel_path.as_posix(),
                sha256=download_result.sha256,
                downloaded_at=now_iso,
                skip_reason=None,
                error=None,
                next_retry_at=None,
                attempts=attempts,
                last_attempt_at=now_iso,
            )
            return "downloaded", None
        except FileTooLargeError as exc:
            self.repo.update_file_sync_state(
                user_id,
                file_id,
                status="ignored",
                skip_reason="muito_grande",
                error=str(exc),
                attempts=attempts,
                last_attempt_at=now_iso,
            )
            return "ignored", "muito_grande"
        except DownloadError as exc:
            hours = (
                self.config.blocked_retry_hours
                if exc.status_code in (401, 403)
                else self.config.download_retry_hours
            )
            retry_at = to_utc_iso(now_dt + timedelta(hours=hours)) if exc.retryable else None
            self.repo.update_file_sync_state(
                user_id,
                file_id,
                status="failed",
                error=str(exc),
                next_retry_at=retry_at,
                attempts=attempts,
                last_attempt_at=now_iso,
            )
            return "failed", str(exc)
        except Exception as exc:
            logger.exception("Erro inesperado ao baixar o arquivo %s: %s", file_id, exc)
            retry_at = to_utc_iso(now_dt + timedelta(hours=self.config.download_retry_hours))
            self.repo.update_file_sync_state(
                user_id,
                file_id,
                status="failed",
                error=str(exc),
                next_retry_at=retry_at,
                attempts=attempts,
                last_attempt_at=now_iso,
            )
            return "failed", str(exc)
