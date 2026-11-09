from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import UserProfile
from ..repository import Repository
from .chunking import Chunk, build_chunks
from .extractors import ExtractionResult, extract_file

logger = logging.getLogger(__name__)


@dataclass
class ExtractionRunResult:
    total_files: int = 0
    processed_files: int = 0
    skipped_files: int = 0
    reused_content: int = 0
    extracted: int = 0
    without_text: int = 0
    failed: int = 0
    total_chunks_created: int = 0
    elapsed_seconds: float = 0.0
    issues: list[str] = field(default_factory=list)

    def as_lines(self) -> list[str]:
        lines = [
            f"arquivos avaliados: {self.total_files}",
            f"processados: {self.processed_files} (reaproveitados por hash: {self.reused_content})",
            f"ignorados/inalterados: {self.skipped_files}",
            f"status extraído: {self.extracted}",
            f"status sem texto: {self.without_text}",
            f"status falha: {self.failed}",
            f"chunks gerados: {self.total_chunks_created}",
            f"tempo decorrido: {self.elapsed_seconds:.2f}s",
        ]
        if self.issues:
            lines.append("avisos de falha:")
            lines.extend(f"- {issue}" for issue in self.issues[:10])
        return lines


class ExtractionPipeline:
    def __init__(
        self,
        repo: Repository,
        user: UserProfile,
        files_dir: Path,
    ) -> None:
        self.repo = repo
        self.user = user
        self.files_dir = files_dir

    def run(
        self, *, force_all: bool = False, course_ids: list[int] | None = None
    ) -> ExtractionRunResult:
        start_time = time.perf_counter()
        result = ExtractionRunResult()

        files = self.repo.get_downloaded_files_for_extraction(
            self.user.user_id, course_ids=course_ids
        )
        result.total_files = len(files)

        for row in files:
            file_id = int(row["file_id"])
            course_id = int(row["course_id"])
            sha256 = row.get("sha256")
            rel_path = row.get("local_path")
            display_name = row.get("display_name") or f"arquivo_{file_id}"

            if not sha256 or not rel_path:
                result.skipped_files += 1
                continue

            full_path = self.files_dir / Path(rel_path)
            if not full_path.is_file():
                logger.warning("Arquivo físico ausente no disco: %s", full_path)
                result.failed += 1
                result.issues.append(f"{display_name}: arquivo ausente em {rel_path}")
                continue

            extracted_record = self.repo.get_extracted_content(sha256)
            existing_chunks_count = int(row.get("existing_chunks_count") or 0)

            # Idempotência: se já foi extraído, tem mesmo hash e chunks já batem
            if not force_all and extracted_record is not None:
                if extracted_record["status"] in ("sem_texto", "falha"):
                    result.skipped_files += 1
                    continue
                if extracted_record["status"] == "extraido" and existing_chunks_count > 0:
                    result.skipped_files += 1
                    continue

                # Se o hash já foi extraído por outra matéria ou antes, copia os chunks
                if extracted_record["status"] == "extraido" and existing_chunks_count == 0:
                    copied = self.repo.copy_chunks_for_file(
                        self.user.user_id,
                        course_id,
                        file_id,
                        sha256,
                    )
                    result.processed_files += 1
                    result.reused_content += 1
                    result.extracted += 1
                    result.total_chunks_created += copied
                    continue

            result.processed_files += 1

            # Executa a extração no disco
            try:
                ext_result: ExtractionResult = extract_file(full_path)
            except Exception as exc:
                logger.exception("Falha inesperada no extrator de %s", display_name)
                ext_result = ExtractionResult(status="falha", units=[], error_message=str(exc))

            if ext_result.status == "falha":
                result.failed += 1
                result.issues.append(f"{display_name}: {ext_result.error_message}")
                self.repo.save_extracted_content(
                    sha256,
                    status="falha",
                    error_message=ext_result.error_message,
                    total_chunks=0,
                    total_characters=0,
                    empty_units_count=ext_result.empty_units_count,
                )
                self.repo.delete_file_chunks(self.user.user_id, file_id)
                continue

            if ext_result.status == "sem_texto":
                result.without_text += 1
                self.repo.save_extracted_content(
                    sha256,
                    status="sem_texto",
                    error_message=ext_result.error_message,
                    total_chunks=0,
                    total_characters=0,
                    empty_units_count=ext_result.empty_units_count,
                )
                self.repo.delete_file_chunks(self.user.user_id, file_id)
                continue

            # Status 'extraido' -> constrói chunks
            chunks: list[Chunk] = build_chunks(ext_result.units)
            chunk_dicts = [
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

            self.repo.save_extracted_content(
                sha256,
                status="extraido",
                error_message=None,
                total_chunks=len(chunks),
                total_characters=ext_result.total_characters,
                empty_units_count=ext_result.empty_units_count,
            )
            self.repo.replace_file_chunks(
                self.user.user_id,
                course_id,
                file_id,
                sha256,
                chunk_dicts,
            )

            result.extracted += 1
            result.total_chunks_created += len(chunks)

        result.elapsed_seconds = time.perf_counter() - start_time
        return result
