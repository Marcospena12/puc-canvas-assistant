from __future__ import annotations

from datetime import datetime

from canvas_assistant.backup import check_backup, create_backup, list_backups
from canvas_assistant.db import Database


def test_backup_is_consistent_and_retention(tmp_path) -> None:
    db_path = tmp_path / "assistant.sqlite3"
    db = Database(db_path)
    db.initialize()

    backup_dir = tmp_path / "backups"
    first = create_backup(
        db_path,
        backup_dir,
        keep=2,
        now=datetime(2026, 1, 1, 10, 0, 0),
    )
    second = create_backup(
        db_path,
        backup_dir,
        keep=2,
        now=datetime(2026, 1, 2, 10, 0, 0),
    )
    third = create_backup(
        db_path,
        backup_dir,
        keep=2,
        now=datetime(2026, 1, 3, 10, 0, 0),
    )

    assert first.integrity_ok is True
    assert second.integrity_ok is True
    assert third.integrity_ok is True
    assert check_backup(third.path) is True
    assert len(list_backups(backup_dir)) == 2
    assert not first.path.exists()
