from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "assistant-"


@dataclass(frozen=True, slots=True)
class BackupResult:
    path: Path
    size_bytes: int
    integrity_ok: bool
    removed: int = 0


def _timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def create_backup(
    db_path: Path,
    backup_dir: Path,
    *,
    keep: int = 7,
    now: datetime | None = None,
) -> BackupResult:
    if not db_path.exists():
        raise FileNotFoundError(f"Banco não encontrado: {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{BACKUP_PREFIX}{_timestamp(now)}.sqlite3"

    source = sqlite3.connect(db_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()

    integrity_ok = check_backup(target)
    removed = _prune(backup_dir, keep=keep)
    result = BackupResult(
        path=target,
        size_bytes=target.stat().st_size,
        integrity_ok=integrity_ok,
        removed=removed,
    )
    logger.info(
        "Backup criado: %s (%s bytes, integrity=%s, removidos=%s)",
        target,
        result.size_bytes,
        integrity_ok,
        removed,
    )
    return result


def check_backup(path: Path) -> bool:
    if not path.exists():
        return False
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and row[0] == "ok")
    except sqlite3.Error:
        logger.exception("Falha ao verificar backup %s", path)
        return False
    finally:
        conn.close()


def list_backups(backup_dir: Path) -> list[Path]:
    if not backup_dir.exists():
        return []
    return sorted(
        backup_dir.glob(f"{BACKUP_PREFIX}*.sqlite3"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def latest_backup(backup_dir: Path) -> Path | None:
    backups = list_backups(backup_dir)
    return backups[0] if backups else None


def _prune(backup_dir: Path, *, keep: int) -> int:
    keep = max(1, keep)
    backups = list_backups(backup_dir)
    removed = 0
    for old in backups[keep:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            logger.warning("Não foi possível remover backup antigo %s", old)
    return removed
