from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from canvas_assistant.canvas.errors import CanvasForbiddenError
from canvas_assistant.config import FileSyncConfig, NotificationConfig
from canvas_assistant.knowledge.downloader import DownloadError, DownloadResult
from canvas_assistant.knowledge.file_sync import (
    FileSyncService,
    detect_file_extension,
    sanitize_filename,
)
from canvas_assistant.knowledge.report import (
    build_files_text,
    resolve_course,
)
from canvas_assistant.models import CourseFile, Module, ModuleItem
from canvas_assistant.notifications import NotificationService
from canvas_assistant.sync import SyncService
from canvas_assistant.timeutil import to_utc_iso, utc_now


def make_notification_config(**overrides) -> NotificationConfig:
    from datetime import time

    values = {
        "enabled": True,
        "new_assignments": True,
        "due_hours": (24, 2),
        "announcements": True,
        "new_files": True,
        "digest": True,
        "digest_time": time(8, 0),
        "quiet_start": time(22, 0),
        "quiet_end": time(7, 0),
        "max_stale_minutes": 45,
        "digest_grace_minutes": 120,
    }
    values.update(overrides)
    return NotificationConfig(**values)


class FakeNotifChannel:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, user, text: str) -> bool:
        self.messages.append(text)
        return True


class FakeDownloader:
    def __init__(
        self,
        contents: dict[str, bytes] | None = None,
        *,
        errors: dict[str, Exception] | None = None,
    ) -> None:
        self.contents = contents or {}
        self.errors = errors or {}
        self.calls: list[str] = []

    async def download(
        self,
        url: str,
        dest: Path,
        *,
        max_bytes: int,
        expected_size: int | None = None,
    ) -> DownloadResult:
        self.calls.append(url)
        if url in self.errors:
            raise self.errors[url]
        data = self.contents.get(url, b"default fake content")
        if len(data) > max_bytes:
            raise DownloadError("muito grande", retryable=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        hasher = hashlib.sha256(data)
        return DownloadResult(sha256=hasher.hexdigest(), size=len(data), path=dest)


class FakeCanvas:
    def __init__(
        self,
        *,
        files_by_course: dict[int, list[dict[str, Any]]] | None = None,
        file_details: dict[int, dict[str, Any]] | None = None,
        folders_by_course: dict[int, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.files_by_course = files_by_course or {}
        self.file_details = file_details or {}
        self.folders_by_course = folders_by_course or {}
        self.files_calls: list[int] = []

    async def get_files(self, course_id: int) -> list[dict[str, Any]]:
        self.files_calls.append(course_id)
        if course_id in self.files_by_course:
            val = self.files_by_course[course_id]
            if isinstance(val, Exception):
                raise val
            return val
        return []

    async def get_file(self, course_id: int, file_id: int) -> dict[str, Any]:
        if file_id in self.file_details:
            val = self.file_details[file_id]
            if isinstance(val, Exception):
                raise val
            return val
        return {
            "id": file_id,
            "display_name": f"detalhe_{file_id}.pdf",
            "filename": f"detalhe_{file_id}.pdf",
            "content-type": "application/pdf",
            "size": 128,
            "url": f"https://canvas.test/files/{file_id}/download",
            "modified_at": to_utc_iso(utc_now()),
        }

    async def get_folders(self, course_id: int) -> list[dict[str, Any]]:
        return self.folders_by_course.get(course_id, [])

    async def get_assignments(self, course_id: int) -> list[dict[str, Any]]:
        return []

    async def get_announcements(self, course_id: int, start: str, end: str) -> list[dict[str, Any]]:
        return []

    async def get_modules(self, course_id: int) -> list[dict[str, Any]]:
        return []


def make_file_sync_config(tmp_path: Path, **overrides) -> FileSyncConfig:
    data = {
        "enabled": True,
        "interval_minutes": 60,
        "files_dir": tmp_path / "files",
        "max_size_mb": 1,
        "allowed_types": frozenset({"pdf", "pptx", "docx", "txt", "md"}),
        "blocked_retry_hours": 24,
        "download_retry_hours": 6,
        "download_timeout_seconds": 10,
    }
    data.update(overrides)
    return FileSyncConfig(**data)


def seed_file_item(
    repo,
    user,
    course,
    *,
    file_id: int = 10,
    display_name: str = "Aula 1.pdf",
    content_type: str = "application/pdf",
    size: int = 256,
    url: str | None = None,
    hidden: bool = False,
    locked: bool = False,
    folder_id: int | None = 1,
    updated_at: str | None = None,
) -> CourseFile:
    file = CourseFile(
        id=file_id,
        course_id=course.id,
        display_name=display_name,
        filename=display_name,
        content_type=content_type,
        size=size,
        created_at=updated_at or to_utc_iso(utc_now()),
        updated_at=updated_at or to_utc_iso(utc_now()),
        hidden=hidden,
        locked=locked,
        url=url or f"https://canvas.test/files/{file_id}/download",
        mime_class="pdf",
        folder_id=folder_id,
    )
    repo.replace_files(user.user_id, course.id, [file])
    return file


async def test_sanitize_filename_and_detect_extension() -> None:
    assert sanitize_filename("Aula 01: Introdução <BD>?.pdf") == "Aula 01_ Introdução _BD__.pdf"
    assert sanitize_filename("CON.pdf") == "_CON.pdf"
    assert sanitize_filename("   ") == "arquivo"

    assert detect_file_extension("aula.PDF", None) == "pdf"
    assert detect_file_extension("sem_extensao", "application/pdf") == "pdf"
    assert detect_file_extension("slides.pptx", None) == "pptx"
    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert detect_file_extension("documento", docx_mime) == "docx"
    assert detect_file_extension("imagem.png", "image/png") == "png"


async def test_file_sync_unifies_files_and_modules(
    tmp_path, repo, user, tz, course_factory
) -> None:
    course = course_factory(id=1, name="Bancos de Dados")
    repo.upsert_courses(user.user_id, [course])

    seed_file_item(repo, user, course, file_id=10, display_name="Aula 1.pdf")

    repo.replace_modules(
        user.user_id,
        course.id,
        [
            Module(
                id=100,
                course_id=course.id,
                name="Unidade 1",
                position=1,
                state="completed",
                unlock_at=None,
                require_sequential_progress=False,
                items_count=2,
            )
        ],
        [
            ModuleItem(
                id=1,
                course_id=course.id,
                module_id=100,
                title="Aula 1.pdf",
                type="File",
                position=1,
                html_url=None,
                url=None,
                external_url=None,
                page_url=None,
                content_id=10,
            ),
            ModuleItem(
                id=2,
                course_id=course.id,
                module_id=100,
                title="Aula 2 exclusiva.pdf",
                type="File",
                position=2,
                html_url=None,
                url=None,
                external_url=None,
                page_url=None,
                content_id=20,
            ),
        ],
    )

    canvas = FakeCanvas(
        folders_by_course={1: [{"id": 1, "name": "Aulas"}]},
        file_details={
            20: {
                "id": 20,
                "display_name": "Aula 2 exclusiva.pdf",
                "filename": "aula2.pdf",
                "content-type": "application/pdf",
                "size": 512,
                "url": "https://canvas.test/files/20/download",
                "modified_at": to_utc_iso(utc_now()),
                "folder_id": 1,
            }
        },
    )
    downloader = FakeDownloader(
        {
            "https://canvas.test/files/10/download": b"conteudo aula 1",
            "https://canvas.test/files/20/download": b"conteudo aula 2",
        }
    )
    config = make_file_sync_config(tmp_path)
    service = FileSyncService(repo, canvas, user, tz, config, downloader)

    result = await service.sync_all(download=True)

    assert result.files_seen == 2
    assert result.downloaded == 2
    assert result.new_files == 2

    states = repo.get_file_sync_states(user.user_id, course.id)
    assert set(states.keys()) == {10, 20}

    assert states[10]["source"] == "both"
    assert states[10]["module_name"] == "Unidade 1"
    assert states[10]["folder_name"] == "Aulas"
    assert states[10]["status"] == "downloaded"
    assert states[10]["local_path"] == "1/10_Aula 1.pdf"
    assert (config.files_dir / "1" / "10_Aula 1.pdf").read_bytes() == b"conteudo aula 1"

    assert states[20]["source"] == "modules"
    assert states[20]["module_name"] == "Unidade 1"
    assert states[20]["status"] == "downloaded"


async def test_file_sync_is_incremental_and_idempotent(
    tmp_path, repo, user, tz, course_factory
) -> None:
    course = course_factory(id=1, name="Cálculo")
    repo.upsert_courses(user.user_id, [course])
    seed_file_item(
        repo,
        user,
        course,
        file_id=10,
        display_name="Lista.pdf",
        size=100,
        updated_at="2026-11-01T12:00:00+00:00",
    )

    url = "https://canvas.test/files/10/download"
    downloader = FakeDownloader({url: b"v1 de conteudo"})
    config = make_file_sync_config(tmp_path)
    service = FileSyncService(repo, FakeCanvas(), user, tz, config, downloader)

    first = await service.sync_all(download=True)
    assert first.downloaded == 1
    assert first.unchanged == 0
    assert len(downloader.calls) == 1

    second = await service.sync_all(download=True)
    assert second.downloaded == 0
    assert second.unchanged == 1
    assert len(downloader.calls) == 1

    seed_file_item(
        repo,
        user,
        course,
        file_id=10,
        display_name="Lista.pdf",
        size=200,
        updated_at="2026-11-02T12:00:00+00:00",
    )
    downloader.contents[url] = b"v2 de conteudo atualizado"

    third = await service.sync_all(download=True)
    assert third.downloaded == 1
    assert third.unchanged == 0
    assert len(downloader.calls) == 2

    dest = config.files_dir / "1" / "10_Lista.pdf"
    assert dest.read_bytes() == b"v2 de conteudo atualizado"


async def test_file_sync_ignores_unsupported_types_and_large_files(
    tmp_path, repo, user, tz, course_factory
) -> None:
    course = course_factory(id=1, name="Multimídia")
    repo.upsert_courses(user.user_id, [course])

    files = [
        CourseFile(
            id=1,
            course_id=course.id,
            display_name="foto.png",
            filename="foto.png",
            content_type="image/png",
            size=100,
            created_at=None,
            updated_at=None,
            hidden=False,
            locked=False,
            url="https://canvas.test/files/1",
            mime_class="image",
        ),
        CourseFile(
            id=2,
            course_id=course.id,
            display_name="pesado.pdf",
            filename="pesado.pdf",
            content_type="application/pdf",
            size=10 * 1024 * 1024,
            created_at=None,
            updated_at=None,
            hidden=False,
            locked=False,
            url="https://canvas.test/files/2",
            mime_class="pdf",
        ),
        CourseFile(
            id=3,
            course_id=course.id,
            display_name="oculto.pdf",
            filename="oculto.pdf",
            content_type="application/pdf",
            size=100,
            created_at=None,
            updated_at=None,
            hidden=True,
            locked=False,
            url="https://canvas.test/files/3",
            mime_class="pdf",
        ),
        CourseFile(
            id=4,
            course_id=course.id,
            display_name="bloqueado.pdf",
            filename="bloqueado.pdf",
            content_type="application/pdf",
            size=100,
            created_at=None,
            updated_at=None,
            hidden=False,
            locked=True,
            url="https://canvas.test/files/4",
            mime_class="pdf",
        ),
    ]
    repo.replace_files(user.user_id, course.id, files)

    downloader = FakeDownloader()
    config = make_file_sync_config(tmp_path, max_size_mb=1)
    service = FileSyncService(repo, FakeCanvas(), user, tz, config, downloader)

    result = await service.sync_all(download=True)

    assert result.files_seen == 4
    assert result.downloaded == 0
    assert result.ignored == 4
    assert len(downloader.calls) == 0

    states = repo.get_file_sync_states(user.user_id, course.id)
    assert states[1]["skip_reason"].startswith("tipo_nao_suportado")
    assert states[2]["skip_reason"] == "muito_grande"
    assert states[3]["skip_reason"] == "hidden"
    assert states[4]["skip_reason"] == "locked"


async def test_file_sync_handles_individual_file_failure(
    tmp_path, repo, user, tz, course_factory
) -> None:
    course = course_factory(id=1, name="Sistemas")
    repo.upsert_courses(user.user_id, [course])

    files = [
        CourseFile(
            id=1,
            course_id=course.id,
            display_name="ok.pdf",
            filename="ok.pdf",
            content_type="application/pdf",
            size=100,
            created_at=None,
            updated_at=None,
            hidden=False,
            locked=False,
            url="https://canvas.test/files/1",
            mime_class="pdf",
        ),
        CourseFile(
            id=2,
            course_id=course.id,
            display_name="falha.pdf",
            filename="falha.pdf",
            content_type="application/pdf",
            size=100,
            created_at=None,
            updated_at=None,
            hidden=False,
            locked=False,
            url="https://canvas.test/files/2",
            mime_class="pdf",
        ),
    ]
    repo.replace_files(user.user_id, course.id, files)

    downloader = FakeDownloader(
        contents={"https://canvas.test/files/1": b"ok"},
        errors={
            "https://canvas.test/files/2": DownloadError("servidor indisponivel", status_code=503)
        },
    )
    config = make_file_sync_config(tmp_path)
    service = FileSyncService(repo, FakeCanvas(), user, tz, config, downloader)

    result = await service.sync_all(download=True)

    assert result.downloaded == 1
    assert result.failed == 1

    states = repo.get_file_sync_states(user.user_id, course.id)
    assert states[1]["status"] == "downloaded"
    assert states[2]["status"] == "failed"
    assert states[2]["next_retry_at"] is not None


async def test_sync_service_blocks_forbidden_files_tab_and_backs_off(
    repo, user, tz, course_factory
) -> None:
    course = course_factory(
        id=99,
        name="Curso Bloqueado",
        tabs=[{"id": "files", "label": "Arquivos", "hidden": False}],
    )
    repo.upsert_courses(user.user_id, [course])

    canvas = FakeCanvas(files_by_course={99: CanvasForbiddenError("403 Proibido", status_code=403)})
    sync = SyncService(canvas, repo, user, tz, file_blocked_retry_hours=24)

    await sync.sync_course_details(course)
    assert len(canvas.files_calls) == 1

    course_sync = repo.get_file_course_sync(user.user_id, 99)
    assert course_sync is not None
    assert course_sync["files_tab_status"] == "forbidden"
    assert course_sync["files_retry_at"] is not None

    await sync.sync_course_details(course)
    assert len(canvas.files_calls) == 1

    repo.clear_file_retries(user.user_id)
    await sync.sync_course_details(course)
    assert len(canvas.files_calls) == 2


async def test_notifications_include_files_with_baseline(
    tmp_path, repo, user, tz, course_factory
) -> None:
    course = course_factory(id=1, name="Banco de Dados")
    repo.upsert_courses(user.user_id, [course])

    seed_file_item(repo, user, course, file_id=1, display_name="Primeiro.pdf")
    config = make_file_sync_config(tmp_path)
    downloader = FakeDownloader({"https://canvas.test/files/1/download": b"f1"})
    service = FileSyncService(repo, FakeCanvas(), user, tz, config, downloader)
    await service.sync_all(download=True)

    channel = FakeNotifChannel()
    notif = NotificationService(repo, user, tz, channel, make_notification_config(new_files=True))
    notif.mark_sync_success(utc_now())

    run = await notif.run_cycle(utc_now())
    assert run.baseline is True
    assert channel.messages == []

    seed_file_item(repo, user, course, file_id=2, display_name="Segundo.pdf")
    downloader.contents["https://canvas.test/files/2/download"] = b"f2"
    await service.sync_all(download=True)

    run = await notif.run_cycle(utc_now())
    assert run.sent == 1
    assert len(channel.messages) == 1
    assert "Segundo.pdf" in channel.messages[0]
    assert "Primeiro.pdf" not in channel.messages[0]


async def test_resolve_course_and_build_files_text(repo, user, tz, course_factory) -> None:
    c1 = course_factory(id=1, name="Bancos de Dados - Sistemas de Informação - Campus Betim")
    c2 = course_factory(id=2, name="Cálculo I - Sistemas de Informação")
    repo.upsert_courses(user.user_id, [c1, c2])

    courses = repo.get_courses(user.user_id)
    matched, cand = resolve_course(courses, "calculo")
    assert matched is not None
    assert matched["id"] == 2

    matched_bd, cand_bd = resolve_course(courses, "banco")
    assert matched_bd is not None
    assert matched_bd["id"] == 1

    matched_id, _ = resolve_course(courses, "1")
    assert matched_id is not None
    assert matched_id["id"] == 1

    summary = build_files_text(repo, user, tz, "")
    assert "Arquivos por matéria ativa:" in summary
    assert "Bancos de Dados" in summary

    detail_empty = build_files_text(repo, user, tz, "Cálculo")
    assert "Nenhum arquivo conhecido para Cálculo I." in detail_empty

    repo.upsert_file_sync_states(
        user.user_id,
        [
            {
                "file_id": 10,
                "course_id": 2,
                "display_name": "Ementa.pdf",
                "filename": "ementa.pdf",
                "content_type": "application/pdf",
                "size": 1024,
                "modified_at": to_utc_iso(utc_now()),
                "status": "downloaded",
                "first_seen_at": to_utc_iso(utc_now()),
                "updated_at": to_utc_iso(utc_now()),
                "active": 1,
            }
        ],
    )
    detail_with_file = build_files_text(repo, user, tz, "Cálculo")
    assert "Arquivos de Cálculo I (1):" in detail_with_file
    assert "Ementa.pdf" in detail_with_file
    assert "[baixado]" in detail_with_file
