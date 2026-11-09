from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS courses (
    user_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    name TEXT NOT NULL,
    course_code TEXT NOT NULL DEFAULT '',
    workflow_state TEXT NOT NULL DEFAULT '',
    term_id INTEGER,
    term_name TEXT,
    term_start_at TEXT,
    term_end_at TEXT,
    current_score REAL,
    current_grade TEXT,
    teachers_json TEXT NOT NULL DEFAULT '[]',
    tabs_json TEXT NOT NULL DEFAULT '[]',
    is_subject INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id)
);

CREATE TABLE IF NOT EXISTS assignments (
    user_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    due_at TEXT,
    lock_at TEXT,
    unlock_at TEXT,
    points_possible REAL,
    submission_types_json TEXT NOT NULL DEFAULT '[]',
    is_quiz INTEGER NOT NULL DEFAULT 0,
    html_url TEXT,
    published INTEGER NOT NULL DEFAULT 1,
    submission_state TEXT,
    submission_score REAL,
    submission_grade TEXT,
    submitted_at TEXT,
    graded_at TEXT,
    late INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    excused INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_assignments_due
    ON assignments (user_id, course_id, due_at);

CREATE TABLE IF NOT EXISTS planner_items (
    user_id TEXT NOT NULL,
    plannable_type TEXT NOT NULL,
    plannable_id TEXT NOT NULL,
    course_id INTEGER,
    context_name TEXT,
    title TEXT,
    plannable_date TEXT,
    due_at TEXT,
    points_possible REAL,
    html_url TEXT,
    submitted INTEGER NOT NULL DEFAULT 0,
    graded INTEGER NOT NULL DEFAULT 0,
    late INTEGER NOT NULL DEFAULT 0,
    missing INTEGER NOT NULL DEFAULT 0,
    new_activity INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, plannable_type, plannable_id)
);
CREATE INDEX IF NOT EXISTS idx_planner_date ON planner_items (user_id, plannable_date);

CREATE TABLE IF NOT EXISTS announcements (
    user_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    message_html TEXT,
    author TEXT,
    posted_at TEXT,
    url TEXT,
    read_state TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_announcements_posted
    ON announcements (user_id, course_id, posted_at);

CREATE TABLE IF NOT EXISTS files (
    user_id TEXT NOT NULL,
    id INTEGER NOT NULL,
    course_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    content_type TEXT,
    size INTEGER,
    created_at TEXT,
    updated_at TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    locked INTEGER NOT NULL DEFAULT 0,
    url TEXT,
    mime_class TEXT,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_files_course ON files (user_id, course_id, updated_at);

CREATE TABLE IF NOT EXISTS modules (
    user_id TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    id INTEGER NOT NULL,
    name TEXT NOT NULL,
    position INTEGER,
    state TEXT,
    unlock_at TEXT,
    require_sequential_progress INTEGER NOT NULL DEFAULT 0,
    items_count INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, course_id, id),
    FOREIGN KEY (user_id, course_id) REFERENCES courses (user_id, id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS module_items (
    user_id TEXT NOT NULL,
    course_id INTEGER NOT NULL,
    module_id INTEGER NOT NULL,
    id INTEGER NOT NULL,
    title TEXT NOT NULL,
    type TEXT NOT NULL,
    position INTEGER,
    html_url TEXT,
    url TEXT,
    external_url TEXT,
    page_url TEXT,
    content_id INTEGER,
    synced_at TEXT NOT NULL,
    PRIMARY KEY (user_id, course_id, id),
    FOREIGN KEY (user_id, course_id, module_id)
        REFERENCES modules (user_id, course_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_module_items_type
    ON module_items (user_id, course_id, type);

CREATE TABLE IF NOT EXISTS sync_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    scope_id INTEGER NOT NULL DEFAULT 0,
    status_code INTEGER,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_issues_user
    ON sync_issues (user_id, created_at);
"""

SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA_SQL)
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('version', ?)",
                (str(SCHEMA_VERSION),),
            )
